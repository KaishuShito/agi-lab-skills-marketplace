"""
Surface extraction layer for delta-lint deep scan (Phase 0).

Extracts "surface contracts" from source files using regex:
- WordPress hooks: do_action, add_action, apply_filters, add_filter
- Function definitions with parameter signatures
- Constants: define(), const
- Classes: class definitions, extends, implements
- Global variables

No LLM required. O(N) parallel, cacheable by file content hash.

Each file produces a small JSON (~1% of file size) containing only
its public interface — what it provides and what it expects.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 正規表現パターン — WordPress PHP
# ---------------------------------------------------------------------------

# do_action( 'hook_name', $arg1, $arg2 )
RE_DO_ACTION = re.compile(
    r"""do_action\s*\(\s*['"]([^'"]+)['"]"""
    r"""(?:\s*,\s*(.+?))?\s*\)""",
    re.DOTALL,
)

# add_action( 'hook_name', callback, priority, accepted_args )
RE_ADD_ACTION = re.compile(
    r"""add_action\s*\(\s*['"]([^'"]+)['"]"""
    r"""\s*,\s*(.+?)"""
    r"""(?:\s*,\s*(\d+))?"""
    r"""(?:\s*,\s*(\d+))?"""
    r"""\s*\)""",
    re.DOTALL,
)

# apply_filters( 'filter_name', $value, $extra )
RE_APPLY_FILTERS = re.compile(
    r"""apply_filters\s*\(\s*['"]([^'"]+)['"]"""
    r"""(?:\s*,\s*(.+?))?\s*\)""",
    re.DOTALL,
)

# add_filter( 'filter_name', callback, priority, accepted_args )
RE_ADD_FILTER = re.compile(
    r"""add_filter\s*\(\s*['"]([^'"]+)['"]"""
    r"""\s*,\s*(.+?)"""
    r"""(?:\s*,\s*(\d+))?"""
    r"""(?:\s*,\s*(\d+))?"""
    r"""\s*\)""",
    re.DOTALL,
)

# function definitions
RE_FUNCTION_DEF = re.compile(
    r"""(?:public|private|protected|static|\s)*function\s+"""
    r"""(\w+)\s*\(\s*(.*?)\s*\)""",
    re.DOTALL,
)

# define('CONSTANT_NAME', value)
RE_DEFINE = re.compile(
    r"""define\s*\(\s*['"](\w+)['"]\s*,\s*(.+?)\s*\)""",
)

# const CONSTANT_NAME = value;
RE_CONST = re.compile(
    r"""(?:^|\s)const\s+(\w+)\s*=\s*(.+?)\s*;""",
    re.MULTILINE,
)

# class ClassName extends ParentClass implements InterfaceA, InterfaceB
RE_CLASS = re.compile(
    r"""class\s+(\w+)"""
    r"""(?:\s+extends\s+(\w+))?"""
    r"""(?:\s+implements\s+([\w,\s\\]+))?""",
)

# global $var1, $var2
RE_GLOBAL = re.compile(r"""global\s+(\$\w+(?:\s*,\s*\$\w+)*)""")

# ---------------------------------------------------------------------------
# 正規表現パターン — TypeScript/JavaScript
# ---------------------------------------------------------------------------

RE_TS_IMPORT = re.compile(
    r"""import\s+(?:\{[^}]+\}|\w+|\*\s+as\s+\w+)\s+from\s+['"]([^'"]+)['"]""",
    re.DOTALL,
)
RE_TS_EXPORT = re.compile(
    r"""export\s+(?:default\s+)?(?:class|function|const|interface|type|enum)\s+(\w+)""",
)


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------

@dataclass
class HookEntry:
    """do_action/apply_filters または add_action/add_filter の1エントリ"""
    hook_type: str       # "action" or "filter"
    role: str            # "fire" (do_action/apply_filters) or "listen" (add_action/add_filter)
    name: str            # フック名
    arg_count: int       # 引数の数（fire 側）
    accepted_args: int   # accepted_args パラメータ（listen 側、デフォルト1）
    callback: str        # コールバック名（listen 側のみ）
    priority: int        # 優先度（listen 側のみ）
    line: int            # 行番号
    snippet: str         # 該当行の前後


@dataclass
class FunctionEntry:
    """関数定義"""
    name: str
    param_count: int
    params: str          # パラメータ文字列（シグネチャ）
    visibility: str      # public/private/protected/""
    line: int
    snippet: str


@dataclass
class ConstantEntry:
    """定数定義"""
    name: str
    value: str
    source: str          # "define" or "const"
    line: int


@dataclass
class ClassEntry:
    """クラス定義"""
    name: str
    extends: str
    implements: list[str]
    line: int


@dataclass
class SurfaceContract:
    """1ファイルの表面契約"""
    file_path: str
    file_hash: str
    hooks: list[dict] = field(default_factory=list)
    functions: list[dict] = field(default_factory=list)
    constants: list[dict] = field(default_factory=list)
    classes: list[dict] = field(default_factory=list)
    globals: list[str] = field(default_factory=list)
    ts_imports: list[str] = field(default_factory=list)
    ts_exports: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _count_args(args_str: str) -> int:
    """PHP引数文字列から引数の数を概算する。
    ネストされたカッコ内のカンマは無視する。"""
    if not args_str or not args_str.strip():
        return 0
    depth = 0
    count = 1
    for ch in args_str:
        if ch in ("(", "["):
            depth += 1
        elif ch in (")", "]"):
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def _file_hash(content: str) -> str:
    """ファイル内容の sha256[:12] を返す"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _get_snippet(lines: list[str], line_idx: int, radius: int = 3) -> str:
    """指定行の前後 radius 行を抽出"""
    start = max(0, line_idx - radius)
    end = min(len(lines), line_idx + radius + 1)
    return "\n".join(lines[start:end])


def _line_number(content: str, pos: int) -> int:
    """文字位置から行番号（1-based）を返す"""
    return content[:pos].count("\n") + 1


# ---------------------------------------------------------------------------
# PHP 表面抽出
# ---------------------------------------------------------------------------

def _extract_php(content: str, lines: list[str]) -> dict:
    """PHP ファイルから表面契約を抽出する"""
    hooks = []
    functions = []
    constants = []
    classes = []
    globals_list = []

    # --- フック: do_action ---
    for m in RE_DO_ACTION.finditer(content):
        line = _line_number(content, m.start())
        args_str = m.group(2) or ""
        hooks.append({
            "hook_type": "action",
            "role": "fire",
            "name": m.group(1),
            "arg_count": _count_args(args_str),
            "accepted_args": 0,
            "callback": "",
            "priority": 0,
            "line": line,
            "snippet": _get_snippet(lines, line - 1),
        })

    # --- フック: add_action ---
    for m in RE_ADD_ACTION.finditer(content):
        line = _line_number(content, m.start())
        callback = m.group(2).strip().strip("'\"")
        priority = int(m.group(3)) if m.group(3) else 10
        accepted = int(m.group(4)) if m.group(4) else 1
        hooks.append({
            "hook_type": "action",
            "role": "listen",
            "name": m.group(1),
            "arg_count": 0,
            "accepted_args": accepted,
            "callback": callback,
            "priority": priority,
            "line": line,
            "snippet": _get_snippet(lines, line - 1),
        })

    # --- フック: apply_filters ---
    for m in RE_APPLY_FILTERS.finditer(content):
        line = _line_number(content, m.start())
        args_str = m.group(2) or ""
        hooks.append({
            "hook_type": "filter",
            "role": "fire",
            "name": m.group(1),
            "arg_count": _count_args(args_str),
            "accepted_args": 0,
            "callback": "",
            "priority": 0,
            "line": line,
            "snippet": _get_snippet(lines, line - 1),
        })

    # --- フック: add_filter ---
    for m in RE_ADD_FILTER.finditer(content):
        line = _line_number(content, m.start())
        callback = m.group(2).strip().strip("'\"")
        priority = int(m.group(3)) if m.group(3) else 10
        accepted = int(m.group(4)) if m.group(4) else 1
        hooks.append({
            "hook_type": "filter",
            "role": "listen",
            "name": m.group(1),
            "arg_count": 0,
            "accepted_args": accepted,
            "callback": callback,
            "priority": priority,
            "line": line,
            "snippet": _get_snippet(lines, line - 1),
        })

    # --- 関数定義 ---
    for m in RE_FUNCTION_DEF.finditer(content):
        line = _line_number(content, m.start())
        params_str = m.group(2).strip()
        # 先頭の修飾子を検出
        prefix = content[max(0, m.start() - 30):m.start()]
        visibility = ""
        for vis in ("public", "private", "protected"):
            if vis in prefix:
                visibility = vis
                break
        functions.append({
            "name": m.group(1),
            "param_count": _count_args(params_str),
            "params": params_str[:200],  # 長すぎる場合を切り詰め
            "visibility": visibility,
            "line": line,
            "snippet": _get_snippet(lines, line - 1),
        })

    # --- 定数: define ---
    for m in RE_DEFINE.finditer(content):
        line = _line_number(content, m.start())
        # WordPress の if (!defined('X')) define('X', ...) ガードを検出
        guarded = False
        # 同一行チェック: if(!defined('X')) define('X', ...)
        current_line = lines[line - 1] if line <= len(lines) else ""
        if f"defined" in current_line and ("!" in current_line.split("define")[0]):
            guarded = True
        # 前の行チェック: if (!defined('X')) {\n  define('X', ...)
        elif line >= 2:
            prev_line = lines[line - 2].strip()
            if "defined" in prev_line and ("!" in prev_line or "not" in prev_line.lower()):
                guarded = True
        constants.append({
            "name": m.group(1),
            "value": m.group(2).strip()[:100],
            "source": "define",
            "line": line,
            "guarded": guarded,
        })

    # --- 定数: const ---
    for m in RE_CONST.finditer(content):
        line = _line_number(content, m.start())
        constants.append({
            "name": m.group(1),
            "value": m.group(2).strip()[:100],
            "source": "const",
            "line": line,
        })

    # --- クラス定義 ---
    for m in RE_CLASS.finditer(content):
        line = _line_number(content, m.start())
        implements = []
        if m.group(3):
            implements = [s.strip() for s in m.group(3).split(",") if s.strip()]
        classes.append({
            "name": m.group(1),
            "extends": m.group(2) or "",
            "implements": implements,
            "line": line,
        })

    # --- グローバル変数 ---
    for m in RE_GLOBAL.finditer(content):
        vars_str = m.group(1)
        for v in vars_str.split(","):
            v = v.strip()
            if v and v not in globals_list:
                globals_list.append(v)

    return {
        "hooks": hooks,
        "functions": functions,
        "constants": constants,
        "classes": classes,
        "globals": globals_list,
    }


# ---------------------------------------------------------------------------
# TypeScript/JavaScript 表面抽出
# ---------------------------------------------------------------------------

def _extract_ts(content: str, lines: list[str]) -> dict:
    """TS/JS ファイルから表面契約を抽出する"""
    ts_imports = []
    ts_exports = []
    functions = []
    classes = []
    constants = []

    for m in RE_TS_IMPORT.finditer(content):
        ts_imports.append(m.group(1))

    for m in RE_TS_EXPORT.finditer(content):
        ts_exports.append(m.group(1))

    # TS/JS の関数定義
    for m in re.finditer(
        r"""(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\((.*?)\)""",
        content, re.DOTALL,
    ):
        line = _line_number(content, m.start())
        functions.append({
            "name": m.group(1),
            "param_count": _count_args(m.group(2).strip()),
            "params": m.group(2).strip()[:200],
            "visibility": "",
            "line": line,
            "snippet": _get_snippet(lines, line - 1),
        })

    # TS/JS のクラス定義
    for m in re.finditer(
        r"""class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s]+))?""",
        content,
    ):
        line = _line_number(content, m.start())
        implements = []
        if m.group(3):
            implements = [s.strip() for s in m.group(3).split(",") if s.strip()]
        classes.append({
            "name": m.group(1),
            "extends": m.group(2) or "",
            "implements": implements,
            "line": line,
        })

    # TS/JS の const 定義
    for m in re.finditer(
        r"""(?:export\s+)?const\s+(\w+)\s*[=:]""",
        content,
    ):
        line = _line_number(content, m.start())
        constants.append({
            "name": m.group(1),
            "value": "",
            "source": "const",
            "line": line,
        })

    return {
        "hooks": [],
        "functions": functions,
        "constants": constants,
        "classes": classes,
        "globals": [],
        "ts_imports": ts_imports,
        "ts_exports": ts_exports,
    }


# ---------------------------------------------------------------------------
# キャッシュ
# ---------------------------------------------------------------------------

def _cache_path(repo_path: str, fhash: str) -> Path:
    """キャッシュファイルのパスを返す"""
    return Path(repo_path) / ".delta-lint" / "cache" / "surfaces" / f"{fhash}.json"


def _get_cached_surface(repo_path: str, fhash: str) -> Optional[dict]:
    """キャッシュから表面契約を読み込む"""
    cp = _cache_path(repo_path, fhash)
    if cp.exists():
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return None


def _save_cached_surface(repo_path: str, fhash: str, data: dict) -> None:
    """表面契約をキャッシュに保存する"""
    cp = _cache_path(repo_path, fhash)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# メインAPI
# ---------------------------------------------------------------------------

def extract_surface(repo_path: str, file_path: str,
                    content: Optional[str] = None) -> SurfaceContract:
    """1ファイルから表面契約を抽出する。

    Args:
        repo_path: リポジトリルート
        file_path: ファイルの相対パス
        content: ファイル内容（None の場合はファイルから読み込む）

    Returns:
        SurfaceContract
    """
    if content is None:
        abs_path = Path(repo_path) / file_path
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return SurfaceContract(file_path=file_path, file_hash="")

    fhash = _file_hash(content)

    # キャッシュチェック
    cached = _get_cached_surface(repo_path, fhash)
    if cached is not None:
        cached["file_path"] = file_path
        cached["file_hash"] = fhash
        return SurfaceContract(**cached)

    lines = content.split("\n")
    ext = Path(file_path).suffix.lower()

    if ext == ".php":
        result = _extract_php(content, lines)
    elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        result = _extract_ts(content, lines)
    else:
        result = {
            "hooks": [], "functions": [], "constants": [],
            "classes": [], "globals": [],
        }

    surface = SurfaceContract(
        file_path=file_path,
        file_hash=fhash,
        hooks=result.get("hooks", []),
        functions=result.get("functions", []),
        constants=result.get("constants", []),
        classes=result.get("classes", []),
        globals=result.get("globals", []),
        ts_imports=result.get("ts_imports", []),
        ts_exports=result.get("ts_exports", []),
    )

    # キャッシュ保存
    cache_data = {
        "hooks": surface.hooks,
        "functions": surface.functions,
        "constants": surface.constants,
        "classes": surface.classes,
        "globals": surface.globals,
        "ts_imports": surface.ts_imports,
        "ts_exports": surface.ts_exports,
    }
    _save_cached_surface(repo_path, fhash, cache_data)

    return surface


def extract_surfaces(repo_path: str, file_paths: list[str],
                     verbose: bool = False) -> list[SurfaceContract]:
    """複数ファイルから表面契約を一括抽出する。

    Args:
        repo_path: リポジトリルート
        file_paths: ファイルの相対パスリスト
        verbose: 詳細表示

    Returns:
        SurfaceContract のリスト
    """
    surfaces = []
    cache_hits = 0
    for fp in file_paths:
        surface = extract_surface(repo_path, fp)
        if surface.file_hash:
            surfaces.append(surface)
            # キャッシュヒットかどうかは file_hash の存在で間接判定
    if verbose:
        total_hooks = sum(len(s.hooks) for s in surfaces)
        total_funcs = sum(len(s.functions) for s in surfaces)
        total_consts = sum(len(s.constants) for s in surfaces)
        total_classes = sum(len(s.classes) for s in surfaces)
        print(f"  [deep] Phase 0: {len(surfaces)} files extracted", file=__import__("sys").stderr)
        print(f"    hooks: {total_hooks}, functions: {total_funcs}, "
              f"constants: {total_consts}, classes: {total_classes}",
              file=__import__("sys").stderr)
    return surfaces


def collect_all_source_files(repo_path: str,
                             exclude_dirs: Optional[set[str]] = None) -> list[str]:
    """リポジトリ内の全ソースファイルを収集する。

    vendor, node_modules, wp-admin, wp-includes 等は除外。
    """
    from retrieval import filter_source_files

    if exclude_dirs is None:
        exclude_dirs = {
            "vendor", "node_modules", ".git", "wp-admin", "wp-includes",
            "__pycache__", ".delta-lint", "unused_plugins", "kingsman-data",
        }

    all_files = []
    repo = Path(repo_path)
    for p in repo.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(repo))
            # ディレクトリ除外チェック
            parts = rel.split("/")
            if any(part in exclude_dirs for part in parts):
                continue
            all_files.append(rel)

    return filter_source_files(all_files)
