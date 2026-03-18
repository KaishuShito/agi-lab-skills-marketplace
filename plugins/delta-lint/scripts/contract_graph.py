"""
Contract graph layer for delta-lint deep scan (Phase 1).

Builds an inverted index from surface contracts and detects
structural mismatches automatically — no LLM required.

Detection rules:
1. hook_arg_mismatch: do_action fires N args, add_action accepts M (M < N)
2. filter_arg_mismatch: apply_filters fires N args, add_filter accepts M (M < N)
3. orphan_hook_fired: do_action/apply_filters with no listener
4. orphan_hook_listener: add_action/add_filter with no corresponding fire
5. constant_conflict: same constant defined with different values
6. missing_parent_class: class extends X but X not found in project
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from surface_extractor import SurfaceContract


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------

@dataclass
class SymbolEntry:
    """逆引きインデックスの1エントリ"""
    symbol_name: str
    symbol_type: str      # "action", "filter", "function", "constant", "class"
    file_path: str
    line: int
    role: str             # "provider" or "consumer"
    metadata: dict = field(default_factory=dict)
    snippet: str = ""


@dataclass
class ContractMismatch:
    """Phase 1 で検出された構造的不整合の候補"""
    mismatch_type: str
    symbol_name: str
    provider: SymbolEntry
    consumer: Optional[SymbolEntry]
    severity_hint: str    # "high" | "medium" | "low"
    description: str
    snippet_a: str = ""
    snippet_b: str = ""


# ---------------------------------------------------------------------------
# WordPress コアフック除外リスト
# ---------------------------------------------------------------------------

# WordPress コアが定義する主要フック（orphan 検出から除外）
WP_CORE_HOOKS = {
    "init", "wp_init", "admin_init", "wp_loaded", "template_redirect",
    "wp_enqueue_scripts", "admin_enqueue_scripts", "wp_head", "wp_footer",
    "admin_menu", "admin_bar_menu", "widgets_init", "register_sidebar",
    "save_post", "delete_post", "trash_post", "transition_post_status",
    "pre_get_posts", "the_post", "loop_start", "loop_end",
    "wp_insert_post", "wp_update_post", "wp_trash_post",
    "the_content", "the_title", "the_excerpt", "wp_title",
    "body_class", "post_class", "nav_menu_css_class",
    "plugins_loaded", "after_setup_theme", "wp", "shutdown",
    "activate_plugin", "deactivate_plugin", "switch_theme",
    "wp_login", "wp_logout", "set_current_user", "auth_cookie_valid",
    "wp_ajax_", "wp_ajax_nopriv_", "rest_api_init",
    "customize_register", "customize_preview_init",
    "manage_posts_columns", "manage_pages_columns",
    "add_meta_boxes", "do_meta_boxes",
    "wp_dashboard_setup", "admin_notices", "all_admin_notices",
    "wp_mail", "phpmailer_init", "wp_mail_from",
    "cron_schedules", "wp_schedule_event",
    "upload_mimes", "wp_handle_upload",
    "option_", "pre_option_", "update_option_",
    "user_register", "profile_update", "delete_user",
    "wp_before_admin_bar_render", "admin_bar_init",
    "muplugins_loaded", "registered_taxonomy", "registered_post_type",
    "query_vars", "rewrite_rules_array", "wp_redirect",
    # ACF hooks
    "acf/init", "acf/input/admin_enqueue_scripts",
    "acf/save_post", "acf/update_value",
    # WooCommerce / common plugin hooks
    "woocommerce_init", "woocommerce_loaded",
}


def _is_project_hook(hook_name: str) -> bool:
    """プロジェクト固有のカスタムフックかどうかを判定する。
    km_ プレフィックスや PmWpJson/ 等のプロジェクト固有パターン。"""
    project_prefixes = (
        "km_", "Km_", "KM_",
        "PmWpJson/", "pmwpjson_",
        "pm_", "Pm_",
        "kingsman_",
        "wrqc_",
    )
    return any(hook_name.startswith(p) for p in project_prefixes)


def _is_wp_core_hook(hook_name: str) -> bool:
    """WordPress コアフックかどうかを判定する"""
    if hook_name in WP_CORE_HOOKS:
        return True
    # プレフィックスマッチ（wp_ajax_ 等）
    for prefix in ("wp_ajax_", "wp_ajax_nopriv_", "option_",
                    "pre_option_", "update_option_", "delete_option_"):
        if hook_name.startswith(prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# インデックス構築
# ---------------------------------------------------------------------------

def build_index(surfaces: list[SurfaceContract]) -> dict[str, list[SymbolEntry]]:
    """表面契約から逆引きインデックスを構築する。

    Returns:
        symbol_name -> [SymbolEntry, ...] のマップ
    """
    index: dict[str, list[SymbolEntry]] = {}

    for surface in surfaces:
        fp = surface.file_path

        # フック
        for hook in surface.hooks:
            name = hook["name"]
            if hook["role"] == "fire":
                entry = SymbolEntry(
                    symbol_name=name,
                    symbol_type=hook["hook_type"],
                    file_path=fp,
                    line=hook["line"],
                    role="provider",
                    metadata={"arg_count": hook["arg_count"]},
                    snippet=hook.get("snippet", ""),
                )
            else:
                entry = SymbolEntry(
                    symbol_name=name,
                    symbol_type=hook["hook_type"],
                    file_path=fp,
                    line=hook["line"],
                    role="consumer",
                    metadata={
                        "callback": hook.get("callback", ""),
                        "priority": hook.get("priority", 10),
                        "accepted_args": hook.get("accepted_args", 1),
                    },
                    snippet=hook.get("snippet", ""),
                )
            index.setdefault(name, []).append(entry)

        # 定数
        for const in surface.constants:
            name = const["name"]
            entry = SymbolEntry(
                symbol_name=name,
                symbol_type="constant",
                file_path=fp,
                line=const["line"],
                role="provider",
                metadata={
                    "value": const["value"],
                    "source": const["source"],
                    "guarded": const.get("guarded", False),
                },
            )
            index.setdefault(f"const:{name}", []).append(entry)

        # クラス
        for cls in surface.classes:
            name = cls["name"]
            # クラス定義自体を provider として登録
            entry = SymbolEntry(
                symbol_name=name,
                symbol_type="class",
                file_path=fp,
                line=cls["line"],
                role="provider",
                metadata={
                    "extends": cls.get("extends", ""),
                    "implements": cls.get("implements", []),
                },
            )
            index.setdefault(f"class:{name}", []).append(entry)

            # extends しているクラスを consumer として登録
            parent = cls.get("extends", "")
            if parent:
                consumer = SymbolEntry(
                    symbol_name=parent,
                    symbol_type="class",
                    file_path=fp,
                    line=cls["line"],
                    role="consumer",
                    metadata={"child": name},
                )
                index.setdefault(f"class:{parent}", []).append(consumer)

    return index


# ---------------------------------------------------------------------------
# ミスマッチ検出ルール
# ---------------------------------------------------------------------------

def detect_mismatches(index: dict[str, list[SymbolEntry]],
                      verbose: bool = False) -> list[ContractMismatch]:
    """インデックスから構造的不整合を検出する。

    Returns:
        ContractMismatch のリスト（LLM 検証前の候補）
    """
    candidates: list[ContractMismatch] = []

    for symbol_name, entries in index.items():
        # --- フック系の検出 ---
        if not symbol_name.startswith("const:") and not symbol_name.startswith("class:"):
            providers = [e for e in entries if e.role == "provider"]
            consumers = [e for e in entries if e.role == "consumer"]

            # ルール 1-2: 引数不一致
            # WordPress では accepted_args < fire側引数数 は正常（余分な引数は無視される）
            # 問題なのは accepted_args > fire側引数数 の場合（存在しない引数を期待）
            # ただし WP コアフックは仕様が固定なので除外
            if not _is_wp_core_hook(symbol_name):
                for p in providers:
                    for c in consumers:
                        if p.file_path == c.file_path and p.line == c.line:
                            continue
                        p_args = p.metadata.get("arg_count", 0)
                        c_args = c.metadata.get("accepted_args", 1)
                        # listener が fire 側より多くの引数を期待 → バグ
                        if p_args > 0 and c_args > p_args:
                            hook_type = p.symbol_type
                            mtype = "hook_arg_mismatch" if hook_type == "action" else "filter_arg_mismatch"
                            candidates.append(ContractMismatch(
                                mismatch_type=mtype,
                                symbol_name=symbol_name,
                                provider=p,
                                consumer=c,
                                severity_hint="high",
                                description=(
                                    f"{hook_type} '{symbol_name}': "
                                    f"fire側が {p_args} 引数を渡しているが、"
                                    f"listen側の accepted_args は {c_args}（存在しない引数を期待）"
                                ),
                                snippet_a=p.snippet,
                                snippet_b=c.snippet,
                            ))

            # ルール 3: orphan_hook_fired
            if providers and not consumers and not _is_wp_core_hook(symbol_name):
                # カスタムフックで listener がない
                for p in providers:
                    candidates.append(ContractMismatch(
                        mismatch_type="orphan_hook_fired",
                        symbol_name=symbol_name,
                        provider=p,
                        consumer=None,
                        severity_hint="low",
                        description=(
                            f"フック '{symbol_name}' が発火されているが、"
                            f"リスナーが見つからない（意図的なextension pointの可能性あり）"
                        ),
                        snippet_a=p.snippet,
                    ))

            # ルール 4: orphan_hook_listener
            # カスタムフック（km_ プレフィックス等）のみ対象。
            # サードパーティプラグインのフックは provider がスキャン対象外の可能性が高い
            if consumers and not providers and not _is_wp_core_hook(symbol_name):
                if _is_project_hook(symbol_name):
                    for c in consumers:
                        candidates.append(ContractMismatch(
                            mismatch_type="orphan_hook_listener",
                            symbol_name=symbol_name,
                            provider=c,
                            consumer=None,
                            severity_hint="medium",
                            description=(
                                f"フック '{symbol_name}' のリスナーが登録されているが、"
                                f"対応する do_action/apply_filters が見つからない"
                            ),
                            snippet_a=c.snippet,
                        ))

        # --- 定数衝突 ---
        elif symbol_name.startswith("const:"):
            const_name = symbol_name[6:]
            providers = [e for e in entries if e.role == "provider"]
            if len(providers) >= 2:
                # guarded 定数（if !defined ガード付き）のみの場合はスキップ
                # WordPress では各ファイルが独立に define() するのが正常パターン
                all_guarded = all(
                    p.metadata.get("guarded", False) for p in providers
                )
                if all_guarded:
                    continue

                # 同一ファイル内の再定義（条件分岐で切り替えるパターン）はスキップ
                unique_files = {p.file_path for p in providers}
                if len(unique_files) == 1:
                    continue

                # 3箇所以上のファイルで定義 → 汎用名パターン（SLUG, VERSION 等）
                # WordPress プラグインが独立に定義する正常パターン
                if len(unique_files) >= 3:
                    continue

                # 異なる言語間の定数は衝突しない
                extensions = {p.file_path.rsplit(".", 1)[-1] for p in providers
                              if "." in p.file_path}
                if len(extensions) > 1:
                    continue

                # 同じ定数名で異なる値
                values = {}
                for p in providers:
                    val = p.metadata.get("value", "")
                    values.setdefault(val, []).append(p)
                if len(values) > 1:
                    items = list(values.items())
                    for i in range(len(items)):
                        for j in range(i + 1, len(items)):
                            val_a, entries_a = items[i]
                            val_b, entries_b = items[j]
                            candidates.append(ContractMismatch(
                                mismatch_type="constant_conflict",
                                symbol_name=const_name,
                                provider=entries_a[0],
                                consumer=entries_b[0],
                                severity_hint="high",
                                description=(
                                    f"定数 '{const_name}' が異なる値で定義されている: "
                                    f"{val_a} ({entries_a[0].file_path}:{entries_a[0].line}) vs "
                                    f"{val_b} ({entries_b[0].file_path}:{entries_b[0].line})"
                                ),
                            ))

        # --- 親クラス未定義 ---
        elif symbol_name.startswith("class:"):
            class_name = symbol_name[6:]
            providers = [e for e in entries if e.role == "provider"]
            consumers = [e for e in entries if e.role == "consumer"]

            if consumers and not providers:
                # extends しているが定義が見つからない
                for c in consumers:
                    candidates.append(ContractMismatch(
                        mismatch_type="missing_parent_class",
                        symbol_name=class_name,
                        provider=c,
                        consumer=None,
                        severity_hint="low",
                        description=(
                            f"クラス '{c.metadata.get('child', '?')}' が "
                            f"'{class_name}' を extends しているが、"
                            f"プロジェクト内に定義が見つからない"
                            f"（vendor/外部ライブラリの可能性あり）"
                        ),
                    ))

    if verbose:
        type_counts: dict[str, int] = {}
        for c in candidates:
            type_counts[c.mismatch_type] = type_counts.get(c.mismatch_type, 0) + 1
        print(f"  [deep] Phase 1: {len(candidates)} candidates detected", file=sys.stderr)
        for mtype, count in sorted(type_counts.items()):
            print(f"    {mtype}: {count}", file=sys.stderr)

    return candidates


# ---------------------------------------------------------------------------
# スニペット拡張（Phase 2 で使用）
# ---------------------------------------------------------------------------

def enrich_snippets(candidates: list[ContractMismatch],
                    repo_path: str, radius: int = 10) -> list[ContractMismatch]:
    """候補のスニペットを拡張する。

    Phase 0 では前後3行だが、Phase 2 のLLM検証では前後10行が必要。
    """
    for c in candidates:
        if c.provider and c.provider.file_path:
            c.snippet_a = _read_snippet(
                repo_path, c.provider.file_path, c.provider.line, radius,
            )
        if c.consumer and c.consumer.file_path:
            c.snippet_b = _read_snippet(
                repo_path, c.consumer.file_path, c.consumer.line, radius,
            )
    return candidates


def _read_snippet(repo_path: str, file_path: str,
                  line: int, radius: int = 10) -> str:
    """ファイルの指定行の前後 radius 行を読み取る"""
    abs_path = Path(repo_path) / file_path
    try:
        content = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = content.split("\n")
    start = max(0, line - 1 - radius)
    end = min(len(lines), line - 1 + radius + 1)
    numbered = []
    for i in range(start, end):
        marker = ">>>" if i == line - 1 else "   "
        numbered.append(f"{marker} {i + 1:4d} | {lines[i]}")
    return "\n".join(numbered)
