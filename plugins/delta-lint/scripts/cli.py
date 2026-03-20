#!/usr/bin/env python3
"""
delta-lint MVP — Structural contradiction detector for source code.

Usage:
    # Scan changed files in current repo (diff-based)
    python cli.py scan

    # Scan specific files
    python cli.py scan --files src/server.ts src/router.ts

    # Scan a different repo
    python cli.py scan --repo /path/to/repo

    # Show all severities
    python cli.py scan --severity low

    # Suppress a finding (interactive)
    python cli.py suppress 3

    # List current suppressions
    python cli.py suppress --list

    # Check for expired suppressions
    python cli.py suppress --check

    # Watch mode: auto re-scan on file changes
    python cli.py scan --watch

    # Default (no subcommand) = scan
    python cli.py
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

# Ensure imports work when running from any directory
sys.path.insert(0, str(Path(__file__).parent))

# Load .env from candidate locations (plugin root or repo root; no hardcoded absolute path)
_env_candidates = [
    Path(__file__).parent.parent / ".env",
    Path.cwd() / ".env",
]
if os.environ.get("DELTA_LINT_ENV"):
    _env_candidates.insert(0, Path(os.environ["DELTA_LINT_ENV"]))
for _env_path in _env_candidates:
    if _env_path.exists():
        for line in _env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), value)
        break

from suppress import (
    SuppressEntry,
    compute_finding_hash,
    compute_code_hash,
    load_suppressions,
    save_suppressions,
    validate_why,
    validate_why_type,
    resolve_why_type,
    _extract_line_number,
)
from findings import cmd_findings

# Extracted modules
from cli_utils import (
    _open_dashboard,
    _load_config,
    _load_profile,
    _apply_profile_policy,
    _apply_config_to_parser,
    _find_latest_scan_log,
    _load_scan_log,
    _normalize_scan_axes,
)
from cmd_init import cmd_init
from cmd_scan import (
    cmd_scan,
    cmd_scan_deep,
    cmd_scan_full,
    cmd_watch,
    _recover_existing_findings,
)


# ---------------------------------------------------------------------------
# cmd_debt_loop
# ---------------------------------------------------------------------------

def cmd_debt_loop(args) -> None:
    """Handle debt-loop subcommand."""
    from debt_loop import run_debt_loop

    finding_ids = args.ids.split(",") if args.ids else None
    results = run_debt_loop(
        repo_path=args.repo,
        count=args.count,
        finding_ids=finding_ids,
        model=args.model,
        backend=args.backend,
        base_branch=args.base_branch,
        status_filter=args.status,
        dry_run=args.dry_run,
        verbose=getattr(args, "verbose", False),
    )
    if not any(r["status"] in ("pr_created", "pushed", "dry_run") for r in results):
        sys.exit(1)


# ---------------------------------------------------------------------------
# cmd_config
# ---------------------------------------------------------------------------

def cmd_config(args) -> None:
    """Handle config subcommand."""
    from scoring import export_default_config

    repo_path = str(Path(getattr(args, "repo", ".")).resolve())

    if args.config_command == "init":
        _config_init(repo_path, interactive=not getattr(args, 'no_interactive', False))
    elif args.config_command == "show":
        _config_show(repo_path)
    else:
        print("Usage: delta-lint config {init|show}", file=sys.stderr)
        sys.exit(1)


def _config_init(repo_path: str, interactive: bool = True) -> None:
    """Export default scoring config to .delta-lint/config.json with guided setup."""
    from scoring import export_default_config

    config_path = Path(repo_path) / ".delta-lint" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    # --- Preset selection (interactive) ---
    PRESETS = {
        "api": {
            "description": "API / バックエンド（REST, GraphQL, マイクロサービス）",
            "scoring": {
                "pattern_weight": {
                    "①": 1.0, "②": 1.0, "③": 0.9, "④": 1.0, "⑤": 0.8, "⑥": 0.9,
                },
            },
            "categories": {
                "application": {
                    "patterns": ["src/**", "app/**", "lib/**", "api/**", "server/**",
                                 "routes/**", "controllers/**", "services/**"],
                    "scan_priority": "high",
                    "severity_boost": 0,
                },
                "infrastructure": {
                    "patterns": ["Dockerfile*", ".github/**", "terraform/**",
                                 "k8s/**", "docker-compose*", "*.yml", "*.yaml"],
                    "scan_priority": "low",
                    "severity_boost": -1,
                },
                "test": {
                    "patterns": ["test/**", "tests/**", "**/*.test.*", "**/*.spec.*",
                                 "**/__tests__/**"],
                    "scan_priority": "low",
                    "severity_boost": -1,
                },
            },
        },
        "frontend": {
            "description": "フロントエンド（React, Vue, Angular 等の SPA）",
            "scoring": {
                "pattern_weight": {
                    "①": 0.9, "②": 1.0, "③": 1.0, "④": 0.8, "⑤": 1.0, "⑥": 0.9,
                },
            },
            "categories": {
                "application": {
                    "patterns": ["src/**", "app/**", "components/**", "pages/**",
                                 "views/**", "hooks/**", "stores/**"],
                    "scan_priority": "high",
                    "severity_boost": 0,
                },
                "infrastructure": {
                    "patterns": ["Dockerfile*", ".github/**", "webpack.*",
                                 "vite.*", "next.config.*", "*.config.js",
                                 "*.config.ts", "public/**"],
                    "scan_priority": "low",
                    "severity_boost": -1,
                },
                "test": {
                    "patterns": ["test/**", "tests/**", "**/*.test.*", "**/*.spec.*",
                                 "**/__tests__/**", "cypress/**", "e2e/**"],
                    "scan_priority": "low",
                    "severity_boost": -1,
                },
            },
        },
        "fullstack": {
            "description": "フルスタック（モノレポ、BFF 等）",
            "scoring": {
                "pattern_weight": {
                    "①": 1.0, "②": 1.0, "③": 1.0, "④": 1.0, "⑤": 0.9, "⑥": 0.9,
                },
            },
            "categories": {
                "application": {
                    "patterns": ["src/**", "app/**", "lib/**", "packages/*/src/**",
                                 "server/**", "client/**"],
                    "scan_priority": "high",
                    "severity_boost": 0,
                },
                "infrastructure": {
                    "patterns": ["Dockerfile*", ".github/**", "terraform/**",
                                 "k8s/**", "docker-compose*", "*.config.*"],
                    "scan_priority": "low",
                    "severity_boost": -1,
                },
                "test": {
                    "patterns": ["test/**", "tests/**", "**/*.test.*", "**/*.spec.*",
                                 "**/__tests__/**", "e2e/**"],
                    "scan_priority": "low",
                    "severity_boost": -1,
                },
            },
        },
        "default": {
            "description": "汎用（特にカスタマイズなし）",
            "scoring": {},
            "categories": {},
        },
    }

    preset_key = "default"
    if interactive and sys.stdin.isatty():
        print("\n── δ-lint config init ──\n", file=sys.stderr)
        print("プロジェクトタイプを選んでください:\n", file=sys.stderr)
        keys = list(PRESETS.keys())
        for i, k in enumerate(keys, 1):
            desc = PRESETS[k]["description"]
            print(f"  {i}. {k:12s} — {desc}", file=sys.stderr)
        print(file=sys.stderr)

        try:
            choice = input("番号を入力 [4=default]: ").strip()
            if choice and choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(keys):
                    preset_key = keys[idx]
            print(f"\n  → プリセット: {preset_key}", file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            print("\n  → デフォルトを使用", file=sys.stderr)

    preset = PRESETS[preset_key]

    # --- Merge scoring ---
    existing_scoring = existing.get("scoring", {})
    defaults = export_default_config()
    # Apply preset scoring overrides
    preset_scoring = preset.get("scoring", {})
    for key in defaults:
        if key not in existing_scoring:
            if key in preset_scoring:
                existing_scoring[key] = preset_scoring[key]
            else:
                existing_scoring[key] = defaults[key]
    existing["scoring"] = existing_scoring

    # --- Merge categories ---
    preset_categories = preset.get("categories", {})
    if preset_categories and "categories" not in existing:
        existing["categories"] = preset_categories

    # --- Merge preset name ---
    if "preset" not in existing:
        existing["preset"] = preset_key

    # --- Add optional keys with documentation ---
    if "disabled_patterns" not in existing:
        existing["_comment_disabled_patterns"] = "disabled_patterns: [\"⑦\", \"⑩\"] で特定パターンを無効化"
    if "default_model" not in existing:
        existing["_comment_default_model"] = "default_model: \"claude-sonnet-4-20250514\" でデフォルトモデル変更"

    config_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"\n✅ Config exported: {config_path}", file=sys.stderr)
    if preset_categories:
        cats = list(preset_categories.keys())
        print(f"  カテゴリ: {', '.join(cats)}", file=sys.stderr)
    print("  scoring / categories セクションを編集してチームに合わせてください。", file=sys.stderr)
    print("  disabled_patterns / default_model も追加可能です。", file=sys.stderr)


def _config_show(repo_path: str) -> None:
    """Show current scoring config (defaults + team overrides)."""
    from scoring import load_scoring_config, diff_from_defaults, validate_config

    # Show config sources
    global_path = Path.home() / ".delta-lint" / "config.json"
    local_path = Path(repo_path).resolve() / ".delta-lint" / "config.json"
    print("--- 設定ソース ---", file=sys.stderr)
    print(f"  global: {global_path} {'✅' if global_path.exists() else '(なし)'}", file=sys.stderr)
    print(f"  repo:   {local_path} {'✅' if local_path.exists() else '(なし)'}", file=sys.stderr)
    print(f"  優先度: CLI > profile > repo > global > defaults\n", file=sys.stderr)

    cfg = load_scoring_config(repo_path)
    print(json.dumps({"scoring": cfg.to_dict()}, indent=2, ensure_ascii=False))

    # Show diff from defaults
    diffs = diff_from_defaults(cfg)
    if diffs:
        print("\n--- カスタム設定 ---", file=sys.stderr)
        for section, changes in diffs.items():
            for key, (default_val, custom_val) in changes.items():
                if default_val is not None:
                    print(f"  {section}.{key}: {default_val} → {custom_val}", file=sys.stderr)
                else:
                    print(f"  {section}.{key}: {custom_val} (新規)", file=sys.stderr)
    else:
        print("\n  すべてデフォルト値", file=sys.stderr)

    # Validation warnings
    warnings = validate_config(cfg)
    if warnings:
        print("\n--- 警告 ---", file=sys.stderr)
        for w in warnings:
            print(f"  ⚠ {w}", file=sys.stderr)


# ---------------------------------------------------------------------------
# cmd_view
# ---------------------------------------------------------------------------

def cmd_view(args):
    """Open unified delta-lint dashboard in the browser.

    Always regenerates HTML from data so that template changes, new findings,
    and scan history updates are reflected without needing --regenerate.
    """
    repo_path = Path(args.repo).resolve()
    delta_dir = repo_path / ".delta-lint"

    # Auto-init: if .delta-lint/ doesn't exist, run scan automatically
    if not delta_dir.exists():
        print("📡 データがないため、自動スキャンを開始します...", file=sys.stderr)
        import subprocess as _sp_init
        _sp_init.run(
            [sys.executable, str(Path(__file__).resolve()), "scan",
             "--repo", str(repo_path), "--no-open"],
            cwd=str(repo_path),
        )

    from findings import generate_dashboard

    treemap_json = None
    results_path = delta_dir / "stress-test" / "results.json"
    if results_path.exists():
        from visualize import build_treemap_json
        treemap_json = build_treemap_json(str(results_path))

    has_findings = any((delta_dir / "findings").glob("*.jsonl")) if (delta_dir / "findings").exists() else False

    # Auto-recover: existing_findings.json exists but JSONL is empty
    # (init was interrupted after scan_existing but before JSONL conversion)
    if not has_findings:
        existing_json = delta_dir / "stress-test" / "existing_findings.json"
        if existing_json.exists():
            recovered = _recover_existing_findings(str(repo_path), existing_json)
            if recovered > 0:
                print(f"🔄 中断された init から {recovered} 件の findings を復元しました。", file=sys.stderr)
                has_findings = True

    # Auto-scan: if no data after init/recovery, run a scan
    if not has_findings and treemap_json is None:
        print("📡 findings がないため、スキャンを実行します...", file=sys.stderr)
        import subprocess as _sp_scan
        _sp_scan.run(
            [sys.executable, str(Path(__file__).resolve()), "scan",
             "--repo", str(repo_path), "--no-open"],
            cwd=str(repo_path),
        )
        # Re-check after scan
        has_findings = any((delta_dir / "findings").glob("*.jsonl")) if (delta_dir / "findings").exists() else False
        if results_path.exists():
            from visualize import build_treemap_json as _btj
            treemap_json = _btj(str(results_path))

    # Auto-recover scan_history.jsonl from stress-test data if missing
    scan_history_path = delta_dir / "scan_history.jsonl"
    if not scan_history_path.exists():
        _recovered_history = False
        try:
            from findings import append_scan_history
            import json as _jh
            # Recover from existing_findings.json
            ef_path = delta_dir / "stress-test" / "existing_findings.json"
            if ef_path.exists():
                ef_data = _jh.loads(ef_path.read_text(encoding="utf-8"))
                ef_results = ef_data.get("results", [])
                ef_findings = sum(len(r.get("findings", [])) for r in ef_results if isinstance(r, dict))
                ts = ef_data.get("metadata", {}).get("timestamp", "")
                append_scan_history(
                    str(repo_path),
                    clusters=len(ef_results),
                    findings_count=ef_findings,
                    scan_type="existing",
                    scope="smart", depth="default", lens="default",
                )
                _recovered_history = True
            # Recover from results.json (stress test)
            if results_path.exists():
                r_data = _jh.loads(results_path.read_text(encoding="utf-8"))
                r_results = r_data.get("results", [])
                r_findings = sum(len(r.get("findings", [])) for r in r_results)
                append_scan_history(
                    str(repo_path),
                    clusters=len(r_results),
                    findings_count=r_findings,
                    scan_type="stress",
                    scope="wide", depth="default", lens="stress",
                )
                _recovered_history = True
            if _recovered_history:
                print("🔄 init データからスキャン履歴を復元しました。", file=sys.stderr)
        except Exception:
            pass

    print("⏳ ダッシュボード生成中（スコアリング・git解析）...", file=sys.stderr, flush=True)
    _dash_tpl = getattr(args, '_dashboard_template', "")
    out = generate_dashboard(str(repo_path), treemap_json=treemap_json, dashboard_template=_dash_tpl)
    if out and not getattr(args, 'no_open', False):
        # cmd_view always opens dashboard (force=True), with live reload by default
        live = not getattr(args, 'no_live', False)
        if _open_dashboard(str(out), force=True, live=live):
            print(f"✓ ダッシュボードを開きました: {out}", file=sys.stderr)
        else:
            print(f"✓ ダッシュボード: {out}", file=sys.stderr)
            print("   ブラウザで手動で開いてください", file=sys.stderr)
    else:
        print(f"✓ ダッシュボード: {out}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Suppress
# ---------------------------------------------------------------------------

def cmd_suppress(args):
    """Suppress a finding, list suppressions, or check for expired ones."""
    repo_path = str(Path(args.repo).resolve())

    if args.list:
        _suppress_list(repo_path)
    elif args.check:
        _suppress_check(repo_path)
    elif args.finding_number is not None:
        _suppress_add(repo_path, args)
    else:
        print("Usage: delta-lint suppress <finding-number>", file=sys.stderr)
        print("       delta-lint suppress --list", file=sys.stderr)
        print("       delta-lint suppress --check", file=sys.stderr)
        sys.exit(1)


def _suppress_list(repo_path: str):
    """List all current suppress entries."""
    entries = load_suppressions(repo_path)
    if not entries:
        print("No suppress entries found.")
        return

    print(f"{len(entries)} suppress entry(ies):\n")
    for e in entries:
        files_str = " <-> ".join(e.files)
        print(f"  [{e.id}] パターン {e.pattern} — {files_str}")
        print(f"    種別: {e.why_type}")
        print(f"    理由: {e.why}")
        approval = f"承認: {e.approved_by}" if e.approved_by else "承認: 未承認（自己判断）"
        print(f"    {approval}")
        print(f"    日付: {e.date}, 作成者: {e.author}")
        if e.line_ranges:
            print(f"    行範囲: {', '.join(e.line_ranges)}")
        print()


def _suppress_check(repo_path: str):
    """Check for expired suppress entries."""
    entries = load_suppressions(repo_path)
    if not entries:
        print("No suppress entries found.")
        return

    expired_count = 0
    for entry in entries:
        # Check code_hash by reading current files
        if entry.files:
            file_path = entry.files[0]
            line_num = None
            if entry.line_ranges:
                # Parse first line range "40-50" → 40
                try:
                    line_num = int(entry.line_ranges[0].split("-")[0])
                except (ValueError, IndexError):
                    pass
            current_hash = compute_code_hash(repo_path, file_path, line_num)
            if current_hash != entry.code_hash:
                expired_count += 1
                files_str = " <-> ".join(entry.files)
                print(f"  EXPIRED [{entry.id}] Pattern {entry.pattern} — {files_str}")
                print(f"    code_hash: {entry.code_hash} → {current_hash}")
                print(f"    why: {entry.why}")
                print()

    if expired_count == 0:
        print(f"All {len(entries)} suppress entry(ies) are still valid.")
    else:
        print(f"{expired_count}/{len(entries)} suppress entry(ies) expired.")


def _suppress_add(repo_path: str, args):
    """Interactively suppress a finding."""
    # Load scan log
    if args.scan_log:
        log_path = Path(args.scan_log)
    else:
        log_path = _find_latest_scan_log(repo_path)

    if not log_path or not log_path.exists():
        print("No scan log found. Run a scan first, or use --scan-log <path>.",
              file=sys.stderr)
        sys.exit(1)

    log_data = _load_scan_log(log_path)
    if not log_data:
        sys.exit(1)

    # Get findings from the log (shown findings are what the user sees)
    shown_findings = log_data.get("findings_shown", [])
    if not shown_findings:
        print("No findings in the scan log to suppress.", file=sys.stderr)
        sys.exit(1)

    # Finding number is 1-based (as displayed in output)
    idx = args.finding_number - 1
    if idx < 0 or idx >= len(shown_findings):
        print(f"Finding number {args.finding_number} out of range. "
              f"Log has {len(shown_findings)} shown finding(s).", file=sys.stderr)
        sys.exit(1)

    finding = shown_findings[idx]

    # Display finding summary
    pattern = finding.get("pattern", "?")
    loc = finding.get("location", {})
    file_a = loc.get("file_a", "?")
    file_b = loc.get("file_b", "?")
    contradiction = finding.get("contradiction", "")

    print(f"Finding {args.finding_number}: Pattern {pattern} — {file_a} <-> {file_b}")
    if contradiction:
        print(f'  "{contradiction[:100]}"')
    print()

    # Non-interactive mode
    if args.why and args.why_type:
        why = args.why
        why_type_raw = args.why_type
    else:
        # Interactive input
        why_type_raw = input("Why type? [d]omain / [t]echnical / [p]reference: ").strip()
        if not why_type_raw:
            print("Cancelled.", file=sys.stderr)
            sys.exit(1)

        print()
        why = input("Why is this intentional? (min 20 chars EN / 10 chars JA):\n> ").strip()

    # Validate
    why_err = validate_why_type(why_type_raw)
    if why_err:
        print(f"Error: {why_err}", file=sys.stderr)
        sys.exit(1)

    why_text_err = validate_why(why)
    if why_text_err:
        print(f"Error: {why_text_err}", file=sys.stderr)
        sys.exit(1)

    why_type = resolve_why_type(why_type_raw)

    # Compute hashes
    fhash = compute_finding_hash(finding)

    detail_a = loc.get("detail_a", "")
    detail_b = loc.get("detail_b", "")
    line_a = _extract_line_number(detail_a)
    line_b = _extract_line_number(detail_b)

    # code_hash from file_a's surrounding code
    chash = compute_code_hash(repo_path, file_a, line_a)

    # Build line_ranges
    line_ranges = []
    if line_a is not None:
        line_ranges.append(f"{max(1, line_a - 5)}-{line_a + 5}")
    if line_b is not None:
        line_ranges.append(f"{max(1, line_b - 5)}-{line_b + 5}")

    # Check for duplicate
    existing = load_suppressions(repo_path)
    for e in existing:
        if e.finding_hash == fhash:
            print(f"Already suppressed as [{e.id}].", file=sys.stderr)
            sys.exit(1)

    # Create entry
    author = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    entry = SuppressEntry(
        id=fhash,
        finding_hash=fhash,
        pattern=pattern,
        files=sorted([file_a, file_b]),
        code_hash=chash,
        why=why,
        why_type=why_type,
        date=str(date.today()),
        author=author,
        line_ranges=line_ranges,
        approved_by=getattr(args, 'approved_by', None) or "",
    )

    existing.append(entry)
    saved_path = save_suppressions(repo_path, existing)
    print(f"\nSuppressed as {fhash}. Written to {saved_path}")


# ---------------------------------------------------------------------------
# main — subcommand routing
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="delta-lint: Detect structural contradictions in source code",
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- scan subcommand ---
    scan_parser = subparsers.add_parser("scan", help="Run structural contradiction scan")
    scan_parser.add_argument(
        "--repo", default=".",
        help="Path to git repository (default: current directory)",
    )
    scan_parser.add_argument(
        "--files", nargs="+",
        help="Specific files to scan (overrides git diff detection)",
    )
    scan_parser.add_argument(
        "--diff-target", default="HEAD",
        help="Git ref to diff against (default: HEAD)",
    )
    scan_parser.add_argument(
        "--severity", default="high",
        choices=["high", "medium", "low"],
        help="Minimum severity to display (default: high)",
    )
    scan_parser.add_argument(
        "--format", default="markdown", dest="output_format",
        choices=["markdown", "json"],
        help="Output format (default: markdown)",
    )
    scan_parser.add_argument(
        "--model", default="claude-sonnet-4-20250514",
        help="Model to use for detection",
    )
    scan_parser.add_argument(
        "--log-dir", default=None,
        help="Directory to save full log (default: .delta-lint/ in repo)",
    )
    scan_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show context that would be sent to LLM, without calling it",
    )
    scan_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed progress information",
    )
    scan_parser.add_argument(
        "--semantic", action="store_true",
        help="Enable semantic search: extract implicit assumptions from diff "
             "and find related files beyond import-based 1-hop dependencies. "
             "Uses claude -p (subscription CLI, $0 cost).",
    )
    scan_parser.add_argument(
        "--backend", default="cli",
        choices=["cli", "api"],
        help="LLM backend: cli (claude -p, $0, default) or api (SDK, pay-per-use). "
             "Falls back to api if CLI not available.",
    )
    scan_parser.add_argument(
        "--lang", default="en",
        choices=["en", "ja"],
        help="Output language for finding descriptions (default: en). "
             "Controls contradiction, impact, and internal_evidence fields.",
    )
    scan_parser.add_argument(
        "--for", default=None, dest="persona",
        choices=["engineer", "pm", "qa"],
        help="Output persona: engineer (default), pm (non-technical), qa (test scenarios). "
             "Uses .delta-lint/config.json default if not specified.",
    )
    scan_parser.add_argument(
        "--no-verify", action="store_true", default=False,
        help="Skip Phase 2 verification (faster but higher false positive rate). "
             "By default, findings are verified with a second LLM pass.",
    )
    scan_parser.add_argument(
        "--autofix", action="store_true", default=False,
        help="Generate minimal fix code for each finding. Off by default. "
             "Enable via CLI flag or config.json {\"autofix\": true}.",
    )
    scan_parser.add_argument(
        "--scope", default=None,
        choices=["diff", "smart", "wide", "pr"],
        help="Scan scope: diff (changed files, default), smart (git history priority), "
             "wide (entire codebase, batched), pr (all files changed since base branch). "
             "Replaces --smart flag.",
    )
    scan_parser.add_argument(
        "--base", default=None,
        help="Base branch for --scope pr (default: auto-detect origin/main or GITHUB_BASE_REF)",
    )
    scan_parser.add_argument(
        "--since", default=None,
        help="Time window for file selection, e.g. '3months', '6months', '1year', '90days'. "
             "Works with --scope diff (default: 3months), smart, and pr. "
             "Collects all files changed in the given period from git log.",
    )
    scan_parser.add_argument(
        "--depth", default=None,
        choices=["deep", "graph", "1hop"],
        help="Context depth: default (direct imports only), "
             "deep (follow transitive imports for deeper analysis).",
    )
    scan_parser.add_argument(
        "--lens", default=None,
        choices=["default", "stress", "security"],
        help="Detection lens: default (contradiction+debt patterns), "
             "stress (virtual modification stress-test), "
             "security (security-focused pattern detection).",
    )
    # Legacy aliases (backward compat)
    scan_parser.add_argument(
        "--smart", action="store_true", default=False,
        help=argparse.SUPPRESS,  # hidden: use --scope smart
    )
    scan_parser.add_argument(
        "--full", action="store_true", default=False,
        help=argparse.SUPPRESS,  # hidden: use --lens stress
    )
    scan_parser.add_argument(
        "--diff-only", action="store_true", default=False,
        help="Show only findings where at least one file is in the current diff. "
             "Useful for PR review: focus on what this change broke.",
    )
    scan_parser.add_argument(
        "--no-cache", action="store_true", default=False,
        help="Skip scan result cache. Always call LLM even if same context was scanned before.",
    )
    scan_parser.add_argument(
        "--no-learn", action="store_true", default=False,
        help="Skip auto-learning: don't update sibling_map.yml from findings.",
    )
    scan_parser.add_argument(
        "--no-open", action="store_true", default=False,
        help="Don't auto-open dashboard in browser after scan.",
    )
    scan_parser.add_argument(
        "--baseline", default=None,
        help="Baseline commit/ref for comparison. Only NEW findings (not in baseline) "
             "trigger exit code 1. Useful for gradual adoption on existing codebases.",
    )
    scan_parser.add_argument(
        "--baseline-save", action="store_true", default=False,
        help="Save current scan results as a baseline snapshot (keyed by HEAD commit). "
             "Run this once on main branch to establish a baseline for --baseline comparisons.",
    )
    scan_parser.add_argument(
        "--watch", action="store_true", default=False,
        help="Watch mode: monitor file changes and re-scan automatically. "
             "Press Ctrl+C to stop.",
    )
    scan_parser.add_argument(
        "--watch-interval", type=float, default=3.0,
        help="Polling interval in seconds for watch mode (default: 3.0)",
    )
    scan_parser.add_argument(
        "--profile", "-p", default=None,
        help="Scan profile name (e.g. deep, light, security). "
             "Loads .delta-lint/profiles/<name>.yml or built-in profiles. "
             "Priority: CLI flags > profile > config.json > defaults.",
    )
    scan_parser.add_argument(
        "--deep", action="store_true", default=False,
        help=argparse.SUPPRESS,  # hidden: use --depth deep
    )
    scan_parser.add_argument(
        "--deep-workers", type=int, default=4,
        help="Number of parallel LLM verification workers for deep scan (default: 4)",
    )
    scan_parser.add_argument(
        "--parallel", type=int, default=3,
        help="Number of parallel batch workers for wide/smart/pr scans (default: 3). "
             "Set to 1 for sequential execution.",
    )
    scan_parser.add_argument(
        "--docs", nargs="*", default=None,
        help="Document files to include as specification contract surfaces. "
             "Checks code × document contradictions (e.g., README claims vs actual behavior). "
             "Pass file paths relative to repo root. "
             "Use --docs without arguments to auto-discover (README.md, ARCHITECTURE.md, docs/**/*.md).",
    )

    # --- init subcommand ---
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize delta-lint for a repository (lightweight structure analysis)",
    )
    init_parser.add_argument(
        "--repo", default=".",
        help="Path to git repository (default: current directory)",
    )
    init_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed progress information",
    )

    # --- view subcommand ---
    view_parser = subparsers.add_parser(
        "view",
        help="Open unified delta-lint dashboard in browser",
    )
    view_parser.add_argument(
        "--repo", default=".",
        help="Path to git repository (default: current directory)",
    )
    view_parser.add_argument(
        "--regenerate", action="store_true", default=False,
        help="Regenerate HTML from data even if it already exists",
    )
    view_parser.add_argument(
        "--no-live", action="store_true", default=False,
        dest="no_live",
        help="Open static file instead of live server",
    )

    # --- findings subcommand ---
    find_parser = subparsers.add_parser("findings", help="Track bugs and contradictions (JSONL)")
    find_sub = find_parser.add_subparsers(dest="findings_command")

    # findings add
    fa = find_sub.add_parser("add", help="Record a new finding")
    fa.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")
    fa.add_argument("--id", default=None, help="Finding ID (auto-generated if omitted)")
    fa.add_argument("--repo-name", default=None, help="Repository name (e.g. Codium-ai/pr-agent)")
    fa.add_argument("--file", default=None, help="File path where finding was detected")
    fa.add_argument("--line", type=int, default=None, help="Line number")
    fa.add_argument("--type", default="bug", choices=["bug", "contradiction", "suspicious", "enhancement"])
    fa.add_argument("--finding-severity", default="high", choices=["high", "medium", "low"])
    fa.add_argument("--pattern", default="", help="Contradiction pattern (e.g. ④ Guard Non-Propagation)")
    fa.add_argument("--title", default="", help="Short title")
    fa.add_argument("--description", default="", help="Detailed description")
    fa.add_argument("--status", default="found", help="Initial status")
    fa.add_argument("--url", default="", help="GitHub Issue/PR URL")
    fa.add_argument("--found-by", default="", help="Who/what found it")
    fa.add_argument("--verified", action="store_true", help="Mark as verified")

    # findings list
    fl = find_sub.add_parser("list", help="List findings")
    fl.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")
    fl.add_argument("--repo-name", default=None, help="Filter by repo name")
    fl.add_argument("--status", default=None, help="Filter by status")
    fl.add_argument("--type", default=None, help="Filter by type")
    fl.add_argument("--format", default="markdown", choices=["markdown", "json"])

    # findings update
    fu = find_sub.add_parser("update", help="Update finding status")
    fu.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")
    fu.add_argument("finding_id", help="Finding ID to update")
    fu.add_argument("new_status", help="New status")
    fu.add_argument("--repo-name", default=None, help="Repository name")
    fu.add_argument("--url", default="", help="GitHub URL to attach")

    # findings search
    fs = find_sub.add_parser("search", help="Search findings by keyword")
    fs.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")
    fs.add_argument("query", help="Search keyword")

    # findings stats
    fst = find_sub.add_parser("stats", help="Show summary statistics")
    fst.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")
    fst.add_argument("--repo-name", default=None, help="Filter by repo name")
    fst.add_argument("--format", default="markdown", choices=["markdown", "json"])

    # findings index
    fi = find_sub.add_parser("index", help="Regenerate _index.md")
    fi.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")

    # findings dashboard
    fd = find_sub.add_parser("dashboard", help="Generate HTML dashboard viewable in browser")
    fd.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")

    # findings enrich
    fe = find_sub.add_parser("enrich", help="Enrich findings with git churn/fan-out data")
    fe.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")

    # findings verify-top
    fv = find_sub.add_parser("verify-top", help="Re-verify top 1/3 findings by priority score")
    fv.add_argument("--repo", default=".", help="Base path for .delta-lint/findings/")
    fv.add_argument("--model", default="claude-sonnet-4-20250514", help="LLM model for verification")
    fv.add_argument("--backend", default="cli", choices=["cli", "api"], help="LLM backend")

    # --- config subcommand ---
    config_parser = subparsers.add_parser("config", help="Manage delta-lint configuration")
    config_sub = config_parser.add_subparsers(dest="config_command")

    config_init = config_sub.add_parser(
        "init",
        help="Export default scoring config to .delta-lint/config.json",
    )
    config_init.add_argument(
        "--repo", default=".",
        help="Path to git repository (default: current directory)",
    )
    config_init.add_argument(
        "--no-interactive", action="store_true", default=False,
        help="Skip interactive preset selection, use defaults",
    )

    config_show = config_sub.add_parser(
        "show",
        help="Show current scoring config (defaults + team overrides)",
    )
    config_show.add_argument(
        "--repo", default=".",
        help="Path to git repository (default: current directory)",
    )

    # --- suppress subcommand ---
    sup_parser = subparsers.add_parser("suppress", help="Manage finding suppressions")
    sup_parser.add_argument(
        "finding_number", nargs="?", type=int, default=None,
        help="Finding number to suppress (1-based, from latest scan)",
    )
    sup_parser.add_argument(
        "--repo", default=".",
        help="Path to git repository (default: current directory)",
    )
    sup_parser.add_argument(
        "--list", action="store_true",
        help="List all current suppress entries",
    )
    sup_parser.add_argument(
        "--check", action="store_true",
        help="Check for expired suppress entries",
    )
    sup_parser.add_argument(
        "--scan-log", default=None,
        help="Path to scan log file (default: latest in .delta-lint/)",
    )
    sup_parser.add_argument(
        "--why", default=None,
        help="Reason for suppression (non-interactive mode)",
    )
    sup_parser.add_argument(
        "--why-type", default=None,
        help="Why type: domain/d, technical/t, preference/p (non-interactive mode)",
    )
    sup_parser.add_argument(
        "--approved-by", default=None,
        help="承認者名（未指定 = 未承認 = 自己判断）",
    )

    # --- debt-loop subcommand ---
    dl_parser = subparsers.add_parser(
        "debt-loop",
        help="Automated debt resolution: pick top N findings, create fix PRs",
    )
    dl_parser.add_argument(
        "--repo", default=".",
        help="Path to git repository (default: current directory)",
    )
    dl_parser.add_argument(
        "--count", "-n", type=int, default=3,
        help="Number of findings to process (default: 3)",
    )
    dl_parser.add_argument(
        "--ids", default=None,
        help="Comma-separated finding IDs to fix (overrides priority sort)",
    )
    dl_parser.add_argument(
        "--model", default="claude-sonnet-4-20250514",
        help="LLM model for fix generation",
    )
    dl_parser.add_argument(
        "--backend", default="cli", choices=["cli", "api"],
        help="LLM backend: cli ($0) or api (pay-per-use)",
    )
    dl_parser.add_argument(
        "--base-branch", default=None,
        help="Base branch for fix branches (default: current branch)",
    )
    dl_parser.add_argument(
        "--status", default="found,verified",
        help="Comma-separated statuses to include (default: found,verified)",
    )
    dl_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Generate fixes but don't commit/push/PR",
    )
    dl_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed progress",
    )

    # Load config.json and profile, apply as parser defaults (CLI flags still win)
    # Priority: CLI flags > profile > config.json > argparse defaults
    # Pre-scan argv for --repo and --profile to resolve paths early
    _repo_hint = "."
    _profile_hint = None
    for i, arg in enumerate(sys.argv):
        if arg == "--repo" and i + 1 < len(sys.argv):
            _repo_hint = sys.argv[i + 1]
        if arg in ("--profile", "-p") and i + 1 < len(sys.argv):
            _profile_hint = sys.argv[i + 1]

    # Layer 1: config.json (lowest priority)
    _config = _load_config(_repo_hint)
    if _config:
        _apply_config_to_parser(scan_parser, _config)

    # Layer 2: profile (overrides config.json)
    _profile_data = {}
    if _profile_hint:
        _profile_data = _load_profile(_profile_hint, _repo_hint)
        if _profile_data:
            # Extract config keys (not _profile_policy) for parser defaults
            _profile_config = {k: v for k, v in _profile_data.items()
                               if not k.startswith("_")}
            if _profile_config:
                _apply_config_to_parser(scan_parser, _profile_config)

    args = parser.parse_args()

    # Build retrieval config: config.json ← profile (2-layer merge)
    _retrieval_keys = ("max_context_chars", "max_file_chars", "max_deps_per_file", "min_confidence")
    _rc = {k: _config[k] for k in _retrieval_keys if k in _config}
    if _profile_data:
        _rc.update({k: _profile_data[k] for k in _retrieval_keys if k in _profile_data})
    if _rc:
        args._retrieval_config = _rc

    # Dashboard template: config.json ← profile policy (2-layer)
    _dash_tpl = _config.get("dashboard_template", "")
    if _profile_data:
        _pp = _profile_data.get("_profile_policy", {})
        _dash_tpl = _pp.get("dashboard_template", _dash_tpl)
    if _dash_tpl:
        args._dashboard_template = _dash_tpl

    # Attach profile policy to args for cmd_scan to use
    if _profile_data:
        _apply_profile_policy(args, _profile_data, _repo_hint)

    # Default to scan when no subcommand given (backward compat)
    if args.command is None:
        # Re-parse as scan
        scan_parser.parse_args(sys.argv[1:], namespace=args)
        args.command = "scan"

    if args.command == "scan":
        # Normalize legacy flags to 3-axis model
        _normalize_scan_axes(args)
        if getattr(args, 'watch', False):
            cmd_watch(args)
        elif args._lens == "stress":
            cmd_scan_full(args)
        else:
            cmd_scan(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "view":
        cmd_view(args)
    elif args.command == "suppress":
        cmd_suppress(args)
    elif args.command == "findings":
        cmd_findings(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "debt-loop":
        cmd_debt_loop(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
