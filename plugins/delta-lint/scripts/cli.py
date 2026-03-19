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
from datetime import date
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

from retrieval import get_changed_files, filter_source_files, build_context
from detector import detect
from output import filter_findings, print_results, save_log
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


# ---------------------------------------------------------------------------
# Environment pre-check — auto-install & guided setup
# ---------------------------------------------------------------------------

def _check_environment(backend: str = "cli", verbose: bool = False) -> dict:
    """Check all external dependencies and attempt auto-install if missing.

    Never exits — always finds a way to continue with degraded functionality.
    Returns a dict with resolved settings:
      {"backend": "cli"|"api", "warnings": [...], "degraded": bool}
    """
    import shutil
    import subprocess as _sp

    warnings: list[str] = []
    resolved_backend = backend
    degraded = False

    # --- git (critical — but even without it, --files mode can work) ---
    if not shutil.which("git"):
        warnings.append(
            "git not found. Diff-based scanning disabled. "
            "Use --files to specify files manually. "
            "Install: https://git-scm.com/downloads  "
            "macOS: xcode-select --install  "
            "Ubuntu: sudo apt install git"
        )
        degraded = True

    # --- claude CLI (needed for backend=cli and semantic search) ---
    claude_available = bool(shutil.which("claude"))
    if not claude_available:
        # Attempt auto-install via npm
        if shutil.which("npm"):
            print("claude CLI not found. Attempting install...", file=sys.stderr)
            try:
                r = _sp.run(
                    ["npm", "install", "-g", "@anthropic-ai/claude-code"],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0 and shutil.which("claude"):
                    claude_available = True
                    print("  ✓ claude CLI installed.", file=sys.stderr)
            except (_sp.TimeoutExpired, OSError):
                pass

        if not claude_available:
            warnings.append(
                "claude CLI not available. "
                "Install: npm install -g @anthropic-ai/claude-code"
            )
            resolved_backend = "api"

    # --- API key (needed for backend=api, or as fallback) ---
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not claude_available and not api_key:
        warnings.append(
            "No LLM backend available (no claude CLI, no API key). "
            "Set ANTHROPIC_API_KEY or install claude CLI to enable scanning. "
            "Continuing in dry-run mode."
        )
        degraded = True

    # --- anthropic SDK (optional, improves api backend reliability) ---
    if resolved_backend == "api":
        try:
            import anthropic as _  # noqa: F401
        except ImportError:
            try:
                print("anthropic SDK not found. Attempting install...", file=sys.stderr)
                r = _sp.run(
                    [sys.executable, "-m", "pip", "install", "anthropic"],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0:
                    print("  ✓ anthropic SDK installed.", file=sys.stderr)
                else:
                    warnings.append(
                        "anthropic SDK not installed. Using raw HTTP fallback."
                    )
            except (_sp.TimeoutExpired, OSError):
                warnings.append("anthropic SDK install failed. Using raw HTTP fallback.")

    # --- PyYAML (optional, fallback to JSON) ---
    try:
        import yaml as _  # noqa: F401
    except ImportError:
        try:
            r = _sp.run(
                [sys.executable, "-m", "pip", "install", "pyyaml"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0 and verbose:
                print("  ✓ PyYAML installed.", file=sys.stderr)
        except (_sp.TimeoutExpired, OSError):
            pass  # JSON fallback is fine

    # --- Default branch detection ---
    try:
        r = _sp.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.getcwd(),
        )
        if r.returncode == 0:
            default_branch = r.stdout.strip().replace("refs/remotes/origin/", "")
            if verbose:
                print(f"  Default branch: {default_branch}", file=sys.stderr)
    except (_sp.TimeoutExpired, OSError):
        pass  # HEAD fallback works

    # --- Shallow clone warning ---
    try:
        r = _sp.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "true":
            warnings.append(
                "Shallow clone detected — git history may be incomplete. "
                "Consider: git fetch --unshallow"
            )
    except (_sp.TimeoutExpired, OSError):
        pass

    # --- Print warnings ---
    for w in warnings:
        print(f"  ⚠ {w}", file=sys.stderr)

    return {"backend": resolved_backend, "warnings": warnings, "degraded": degraded}


# ---------------------------------------------------------------------------
# Config file loading
# ---------------------------------------------------------------------------

def _auto_discover_docs(repo_path: str) -> list[str]:
    """Auto-discover document files for code × document contradiction checking.

    Looks for common documentation files: README.md, ARCHITECTURE.md,
    docs/**/*.md, and ADR files (docs/decisions/*.md).
    Returns paths relative to repo root.
    """
    repo = Path(repo_path).resolve()
    candidates = [
        "README.md", "ARCHITECTURE.md", "CONTRIBUTING.md",
        "DEVELOPMENT.md", "DESIGN.md", "API.md",
    ]
    found: list[str] = []
    for c in candidates:
        if (repo / c).exists():
            found.append(c)

    # docs/**/*.md — ADRs, specs, guides
    docs_dir = repo / "docs"
    if docs_dir.is_dir():
        for md in docs_dir.rglob("*.md"):
            rel = str(md.relative_to(repo))
            if rel not in found:
                found.append(rel)

    return found


def _load_json_safe(path: Path) -> dict:
    """Read a JSON file, returning empty dict on missing/invalid."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (override wins on conflict)."""
    merged = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def _load_config(repo_path: str = ".") -> dict:
    """Load config with 2-tier merge: ~/.delta-lint/config.json → .delta-lint/config.json.

    Priority: repo-local > global user > empty dict.
    """
    global_config = _load_json_safe(Path.home() / ".delta-lint" / "config.json")
    local_config = _load_json_safe(
        Path(repo_path).resolve() / ".delta-lint" / "config.json"
    )
    if not global_config:
        return local_config
    if not local_config:
        return global_config
    return _deep_merge(global_config, local_config)


def _load_profile(profile_name: str, repo_path: str = ".") -> dict:
    """Load a scan profile from .delta-lint/profiles/<name>.yml or built-in profiles/.

    Resolution order:
    1. .delta-lint/profiles/<name>.yml (repo-local, user-created)
    2. Built-in profiles/<name>.yml (shipped with delta-lint)

    Returns the profile's 'config' dict merged with 'policy' dict,
    or empty dict if profile not found.
    """
    try:
        import yaml
    except ImportError:
        print("⚠ PyYAML not installed. Profile loading requires PyYAML.", file=sys.stderr)
        return {}

    # 1. Repo-local profile
    repo_profile = Path(repo_path).resolve() / ".delta-lint" / "profiles" / f"{profile_name}.yml"
    # 2. Built-in profile (next to this script)
    builtin_profile = Path(__file__).parent / "profiles" / f"{profile_name}.yml"

    profile_path = None
    if repo_profile.exists():
        profile_path = repo_profile
    elif builtin_profile.exists():
        profile_path = builtin_profile

    if not profile_path:
        print(f"⚠ Profile '{profile_name}' not found.", file=sys.stderr)
        print(f"  Searched: {repo_profile}", file=sys.stderr)
        print(f"           {builtin_profile}", file=sys.stderr)
        # List available profiles
        available = []
        for d in [repo_profile.parent, builtin_profile.parent]:
            if d.exists():
                available.extend(p.stem for p in d.glob("*.yml") if not p.stem.startswith("_"))
        if available:
            print(f"  Available: {', '.join(sorted(set(available)))}", file=sys.stderr)
        return {}

    try:
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠ Failed to load profile '{profile_name}': {e}", file=sys.stderr)
        return {}

    if not isinstance(data, dict):
        return {}

    # Merge config + policy into a flat dict for _apply_config_to_parser
    result = {}
    if "config" in data and isinstance(data["config"], dict):
        result.update(data["config"])
    if "policy" in data and isinstance(data["policy"], dict):
        result["_profile_policy"] = data["policy"]

    return result


def _apply_profile_policy(args, profile: dict, repo_path: str):
    """Apply profile's policy section to the scan args.

    Policy fields (prompt_append, disabled_patterns, etc.) are injected
    into the runtime config rather than argparse defaults.
    """
    policy = profile.get("_profile_policy")
    if not policy:
        return

    # Store profile policy on args for cmd_scan to pick up
    if not hasattr(args, '_profile_policy'):
        args._profile_policy = {}
    args._profile_policy = policy


# ---------------------------------------------------------------------------
# File category matching (#20)
# ---------------------------------------------------------------------------

def _match_file_category(filepath: str, categories: dict) -> str | None:
    """Match a file path against category patterns. Returns category name or None."""
    import fnmatch

    for cat_name, cat_config in categories.items():
        patterns = cat_config.get("patterns", [])
        for pattern in patterns:
            if fnmatch.fnmatch(filepath, pattern):
                return cat_name
    return None


def _apply_category_severity_boost(findings: list[dict], categories: dict,
                                   verbose: bool = False) -> list[dict]:
    """Apply severity_boost from file categories to findings.

    severity_boost: -1 means demote (high→medium, medium→low),
                    +1 means promote (low→medium, medium→high),
                    0 means no change.
    """
    if not categories:
        return findings

    SEVERITY_LEVELS = ["low", "medium", "high"]
    boosted_count = 0

    for f in findings:
        if f.get("parse_error"):
            continue
        loc = f.get("location", {})
        if not isinstance(loc, dict):
            continue

        # Match both files; use the higher-priority (application > infra/test) category
        file_a = loc.get("file_a", "")
        file_b = loc.get("file_b", "")

        boosts = []
        for fp in (file_a, file_b):
            if fp:
                cat = _match_file_category(fp, categories)
                if cat:
                    boost = categories[cat].get("severity_boost", 0)
                    boosts.append(boost)

        if not boosts:
            continue

        # Use the boost closest to 0 (most conservative — if one file is in
        # application and another in test, don't demote)
        boost = min(boosts, key=abs) if any(b == 0 for b in boosts) else max(boosts, key=abs)

        if boost == 0:
            continue

        current_sev = f.get("severity", "medium").lower()
        current_idx = SEVERITY_LEVELS.index(current_sev) if current_sev in SEVERITY_LEVELS else 1
        new_idx = max(0, min(len(SEVERITY_LEVELS) - 1, current_idx + boost))

        if new_idx != current_idx:
            new_sev = SEVERITY_LEVELS[new_idx]
            f["_original_severity"] = current_sev
            f["severity"] = new_sev
            boosted_count += 1

    if verbose and boosted_count:
        print(f"  Category severity boost: {boosted_count} finding(s) adjusted",
              file=sys.stderr)

    return findings


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


def _apply_config_to_parser(parser, config: dict):
    """Override parser defaults with config values. CLI flags still win."""
    mapping = {
        "lang": "lang",
        "backend": "backend",
        "severity": "severity",
        "model": "model",
        "default_model": "model",  # alias: default_model → model
        "verbose": "verbose",
        "semantic": "semantic",
        "autofix": "autofix",
        "diff_target": "diff_target",
        "output_format": "output_format",
        "format": "output_format",       # alias: format → output_format
        "no_learn": "no_learn",
        "no_cache": "no_cache",
        "no_verify": "no_verify",
    }
    new_defaults = {}
    for config_key, dest in mapping.items():
        if config_key in config:
            new_defaults[dest] = config[config_key]
    if new_defaults:
        parser.set_defaults(**new_defaults)


# ---------------------------------------------------------------------------
# Scan log utilities
# ---------------------------------------------------------------------------

def _find_latest_scan_log(repo_path: str) -> Path | None:
    """Find the most recent scan log in .delta-lint/."""
    log_dir = Path(repo_path) / ".delta-lint"
    if not log_dir.exists():
        return None
    logs = sorted(log_dir.glob("delta_lint_*.json"), reverse=True)
    return logs[0] if logs else None


def _load_scan_log(log_path: Path) -> dict | None:
    """Load and parse a scan log file."""
    try:
        return json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading scan log {log_path}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Baseline comparison (#12)
# ---------------------------------------------------------------------------

def _build_baseline_hashes(repo_path: str, baseline_ref: str,
                           verbose: bool = False) -> set[str] | None:
    """Run a scan at the baseline ref and collect finding hashes.

    Uses cached scan logs if available; otherwise scans baseline in a
    temporary worktree (or stash-based approach).

    Returns a set of finding_hash strings, or None on failure.
    """
    import subprocess as _sp

    # Strategy: look for scan logs already stored for the baseline commit
    try:
        result = _sp.run(
            ["git", "rev-parse", baseline_ref],
            capture_output=True, text=True, timeout=10,
            cwd=repo_path,
        )
        if result.returncode != 0:
            print(f"  ⚠ Cannot resolve baseline ref '{baseline_ref}': {result.stderr.strip()}",
                  file=sys.stderr)
            return None
        baseline_sha = result.stdout.strip()[:12]
    except (_sp.TimeoutExpired, OSError):
        return None

    # Check for baseline snapshot file
    snapshot_path = Path(repo_path) / ".delta-lint" / "baselines" / f"{baseline_sha}.json"
    if snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            hashes = set(data.get("finding_hashes", []))
            if verbose:
                print(f"  Baseline loaded from snapshot: {len(hashes)} finding(s)",
                      file=sys.stderr)
            return hashes
        except (OSError, json.JSONDecodeError):
            pass

    # No snapshot — collect hashes from current scan logs
    # (User should run `delta scan --baseline-save` first, or we generate from
    #  the current shown findings as a fallback)
    if verbose:
        print(f"  No baseline snapshot for {baseline_sha}. "
              f"Use current scan to create one with --baseline-save.", file=sys.stderr)
    return None


def _save_baseline_snapshot(repo_path: str, findings: list[dict],
                            verbose: bool = False) -> Path | None:
    """Save current findings as a baseline snapshot keyed by HEAD commit."""
    import subprocess as _sp

    try:
        result = _sp.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=repo_path,
        )
        if result.returncode != 0:
            return None
        head_sha = result.stdout.strip()[:12]
    except (_sp.TimeoutExpired, OSError):
        return None

    baselines_dir = Path(repo_path) / ".delta-lint" / "baselines"
    baselines_dir.mkdir(parents=True, exist_ok=True)

    # Compute finding hashes
    finding_hashes = []
    for f in findings:
        if f.get("parse_error"):
            continue
        fh = _compute_finding_identity(f)
        if fh:
            finding_hashes.append(fh)

    snapshot = {
        "commit": head_sha,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "findings_count": len(finding_hashes),
        "finding_hashes": finding_hashes,
    }

    snapshot_path = baselines_dir / f"{head_sha}.json"
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if verbose:
        print(f"  Baseline snapshot saved: {snapshot_path} ({len(finding_hashes)} finding(s))",
              file=sys.stderr)
    return snapshot_path


def _compute_finding_identity(f: dict) -> str | None:
    """Compute a stable identity for a finding (pattern + sorted file pair).

    More stable than finding_hash from suppress.py because it doesn't
    depend on contradiction text (which varies between LLM runs).
    """
    import hashlib

    loc = f.get("location", {})
    if not isinstance(loc, dict):
        return None
    file_a = loc.get("file_a", "")
    file_b = loc.get("file_b", "")
    pattern = f.get("pattern", "")
    if not file_a and not file_b:
        return None
    files = sorted([file_a, file_b])
    key = f"{files[0]}:{files[1]}:{pattern}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _filter_new_findings(findings: list[dict],
                         baseline_hashes: set[str]) -> tuple[list[dict], int]:
    """Return only findings NOT in the baseline. Returns (new_findings, baseline_count)."""
    new_findings = []
    baseline_count = 0
    for f in findings:
        fh = _compute_finding_identity(f)
        if fh and fh in baseline_hashes:
            baseline_count += 1
        else:
            new_findings.append(f)
    return new_findings, baseline_count


# ---------------------------------------------------------------------------
# cmd_init (lightweight: structure analysis + constraints.yml scaffold)
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Initialize delta-lint for a repository (lightweight)."""
    repo_path = str(Path(args.repo).resolve())

    # Intro animation (skip if --quiet or non-TTY)
    if not getattr(args, 'quiet', False) and sys.stderr.isatty():
        try:
            from intro_animation import run_animation
            run_animation()
        except Exception:
            pass

    print("── δ-lint ── 初期化開始", file=sys.stderr)

    # Step 0.5: Git history analysis → initial sibling map
    print("  Git 履歴を解析中...", file=sys.stderr)
    git_siblings_count = 0
    try:
        from sibling import (
            generate_siblings_from_git_history,
            load_sibling_map,
            save_sibling_map,
            get_git_churn,
        )
        # Generate siblings from co-change history
        new_siblings = generate_siblings_from_git_history(
            repo_path, months=6, min_co_changes=3, verbose=args.verbose,
        )
        if new_siblings:
            existing = load_sibling_map(repo_path)
            all_entries = existing + new_siblings
            save_sibling_map(repo_path, all_entries)
            git_siblings_count = len(new_siblings)

        # Get churn data for display
        churn_data = get_git_churn(repo_path, months=6)
        if churn_data:
            print(f"  📊 よく改修されるファイル TOP 5:", file=sys.stderr)
            for item in churn_data[:5]:
                print(f"    {item['path']} ({item['changes']}回変更)", file=sys.stderr)

        if git_siblings_count:
            print(f"  🔗 co-change ペア: {git_siblings_count} 件を sibling_map に追加", file=sys.stderr)
    except Exception as e:
        if args.verbose:
            print(f"  [warn] Git history analysis failed: {e}", file=sys.stderr)

    # Step 1: Structure analysis (LLM)
    print("  構造解析を実行中（~30秒）...", file=sys.stderr)

    from stress_test import init_lightweight
    structure = init_lightweight(repo_path, verbose=args.verbose)

    modules = structure.get("modules", [])
    hotspots = structure.get("hotspots", [])

    print(f"\n  {len(modules)} モジュール, {len(hotspots)} ホットスポット検出", file=sys.stderr)
    if hotspots:
        print("  🔥 変更リスクが高いファイル:", file=sys.stderr)
        for h in hotspots[:5]:
            path = h.get("path", h.get("file", ""))
            reason = h.get("reason", "")
            print(f"    {path} — {reason}", file=sys.stderr)

    # Show dev_patterns (from git history analysis)
    dev_patterns = structure.get("dev_patterns", [])
    if dev_patterns:
        _PATTERN_ICONS = {
            "bug-prone": "🐛", "expanding": "📈", "refactoring": "🔧",
            "stable": "✅", "single-owner": "👤",
        }
        print(f"\n  📊 開発パターン分析 ({len(dev_patterns)} エリア):", file=sys.stderr)
        for dp in dev_patterns[:8]:
            icon = _PATTERN_ICONS.get(dp.get("pattern", ""), "❓")
            area = dp.get("area", "?")
            pattern = dp.get("pattern", "?")
            risk = dp.get("risk", "")
            print(f"    {icon} {area} [{pattern}]", file=sys.stderr)
            if risk:
                print(f"      → {risk[:120]}", file=sys.stderr)

    # Show constraints info
    constraints_path = Path(repo_path) / ".delta-lint" / "constraints.yml"
    if constraints_path.exists():
        print(f"\n  📋 constraints.yml 生成済み: {constraints_path}", file=sys.stderr)
        print("    ベテランの知識を追記すると scan 精度が上がります", file=sys.stderr)

    # Show sibling map summary
    try:
        from sibling import load_sibling_map as _load_sib
        all_sibs = _load_sib(repo_path)
        if all_sibs:
            git_count = sum(1 for s in all_sibs if s.source == "git-history")
            finding_count = sum(1 for s in all_sibs if s.source == "finding")
            print(f"\n  🔗 sibling_map: {len(all_sibs)} ペア（git履歴: {git_count}, finding: {finding_count}）", file=sys.stderr)
    except Exception:
        pass

    # Show progressive scan coverage
    try:
        from stress_test import load_coverage
        coverage = load_coverage(repo_path)
        n_covered = len(coverage.get("scanned_files", {}))
        total_scans = coverage.get("total_scans", 0)
        if total_scans > 0:
            from stress_test import _list_source_files
            n_total = len(_list_source_files(repo_path))
            pct = round(n_covered / max(n_total, 1) * 100)
            print(f"\n  📈 スキャンカバレッジ: {n_covered}/{n_total} ファイル ({pct}%) — {total_scans}回実行", file=sys.stderr)
            if pct < 100:
                print(f"    → 再実行するとカバレッジが拡大します（未分析エリアを優先）", file=sys.stderr)
    except Exception:
        pass

    # Step 2: Scan existing contradictions in hotspot clusters → findings.jsonl
    #   Progressive: open dashboard early, update as each cluster completes
    if hotspots:
        print("\n  既存コードの矛盾をスキャン中...", file=sys.stderr)
        try:
            from stress_test import scan_existing
            from findings import Finding, generate_id, add_finding, generate_dashboard
            import webbrowser

            repo_name = Path(repo_path).name
            n_saved = 0
            n_findings = 0
            dashboard_opened = False
            all_fids = []
            all_patterns = []

            for result, completed, total in scan_existing(
                structure, repo_path,
                backend="cli", verbose=args.verbose, parallel=3,
                stream=True,
            ):
                # Save findings from this cluster
                cluster_new = 0
                for f in result.get("findings", []):
                    n_findings += 1
                    loc = f.get("location", {})
                    file_a = loc.get("file_a", "")
                    file_b = loc.get("file_b", "")
                    pattern = f.get("pattern", "")
                    title = f.get("contradiction", f.get("title", ""))[:120]
                    fid = generate_id(repo_name, file_a, title,
                                      file_b=file_b, pattern=pattern)
                    # Enrich with git data (churn, fan_out, total_lines)
                    try:
                        from git_enrichment import enrich_finding
                        enrich_finding(f, repo_path)
                    except Exception:
                        pass
                    finding = Finding(
                        id=fid,
                        repo=repo_name,
                        file=file_a,
                        severity=f.get("severity", "medium"),
                        pattern=pattern,
                        title=title,
                        description=f.get("impact", ""),
                        category=f.get("category", "contradiction"),
                        found_by="delta-init",
                        churn_6m=f.get("churn_6m", 0),
                        fan_out=f.get("fan_out", 0),
                        total_lines=f.get("total_lines", 0),
                    )
                    all_fids.append(fid)
                    if pattern:
                        all_patterns.append(pattern)
                    try:
                        add_finding(repo_path, finding)
                        n_saved += 1
                        cluster_new += 1
                    except ValueError:
                        pass  # duplicate — skip

                is_complete = (completed == total)
                progress = {"completed": completed, "total": total, "is_complete": is_complete}

                # Build treemap JSON if results.json exists (from stress test)
                _treemap = None
                _results_json = Path(repo_path) / ".delta-lint" / "stress-test" / "results.json"
                if _results_json.exists():
                    try:
                        from visualize import build_treemap_json
                        _treemap = build_treemap_json(str(_results_json))
                    except Exception:
                        pass

                # Regenerate dashboard with progress info
                _dash_tpl = getattr(args, '_dashboard_template', "")
                dash_path = generate_dashboard(repo_path, scan_progress=progress, treemap_json=_treemap, dashboard_template=_dash_tpl)

                # Open browser on first generation
                if not dashboard_opened:
                    webbrowser.open(f"file://{dash_path}")
                    dashboard_opened = True
                    print(f"  📊 ダッシュボード: {dash_path}", file=sys.stderr)

                print(f"  [{completed}/{total}] {cluster_new} 件検出 (累計 {n_findings} 件)", file=sys.stderr)

            print(f"  🔍 {n_findings} 件検出、{n_saved} 件を findings に記録", file=sys.stderr)

            # Record scan history (with finding_ids for Chao1 coverage estimation)
            try:
                from findings import append_scan_history
                append_scan_history(
                    repo_path,
                    clusters=total,
                    findings_count=n_findings,
                    scan_type="existing",
                    finding_ids=all_fids,
                    patterns_found=all_patterns,
                    scope="smart",
                    depth="1hop",
                    lens="default",
                )
            except Exception:
                pass
        except Exception as e:
            if args.verbose:
                import traceback
                traceback.print_exc()
            print(f"  [warn] Existing scan failed: {e}", file=sys.stderr)

    print("\n── δ-lint ── 初期化完了 ✅", file=sys.stderr)
    print("  次のステップ:", file=sys.stderr)
    print("    delta view          — ダッシュボードを開く", file=sys.stderr)
    print("    delta scan                    — 変更ファイルをスキャン", file=sys.stderr)
    print("    delta scan --scope all        — 全ファイルスキャン", file=sys.stderr)
    print("    delta scan --lens stress      — ストレステスト（地雷マップ生成）", file=sys.stderr)
    print("    delta scan --lens security    — セキュリティ重点スキャン", file=sys.stderr)
    print("    delta init          — 再実行でカバレッジ拡大", file=sys.stderr)


# ---------------------------------------------------------------------------
# cmd_scan_deep (Phase 0-2: surface extraction → contract graph → LLM verify)
# ---------------------------------------------------------------------------

def cmd_scan_deep(args):
    """Run deep structural scan using regex + contract graph + LLM verification."""
    repo_path = str(Path(args.repo).resolve())
    verbose = getattr(args, "verbose", False)
    workers = getattr(args, "deep_workers", 4)

    print("── δ-lint deep scan ──", file=sys.stderr)

    # Phase 0: Surface extraction
    from surface_extractor import extract_surfaces, collect_all_source_files
    all_files = collect_all_source_files(repo_path)
    if not all_files:
        print("No source files found.", file=sys.stderr)
        sys.exit(0)
    print(f"  Phase 0: Extracting surfaces from {len(all_files)} files...", file=sys.stderr)
    surfaces = extract_surfaces(repo_path, all_files, verbose=verbose)

    # Phase 1: Contract graph
    from contract_graph import build_index, detect_mismatches
    index = build_index(surfaces)
    candidates = detect_mismatches(index, verbose=verbose)

    if not candidates:
        print("  No structural mismatches detected.", file=sys.stderr)
        sys.exit(0)

    # Phase 2: LLM verification
    from deep_verifier import verify_all
    findings = verify_all(candidates, repo_path, max_workers=workers, verbose=verbose)

    if not findings:
        print("  All candidates were rejected by verification.", file=sys.stderr)
        sys.exit(0)

    # Phase 3: Output
    print(f"\n  ✓ {len(findings)} findings confirmed\n", file=sys.stderr)

    output_format = getattr(args, "output_format", "markdown")
    if output_format == "json":
        import json as json_mod
        print(json_mod.dumps(findings, indent=2, ensure_ascii=False))
    else:
        for i, f in enumerate(findings, 1):
            loc = f.get("location", {})
            print(f"### [{i}] {f.get('pattern', '?')} {f.get('severity', '?').upper()}")
            print(f"**{loc.get('file_a', '?')}** {loc.get('detail_a', '')}")
            if loc.get("file_b"):
                print(f"  ↔ **{loc.get('file_b')}** {loc.get('detail_b', '')}")
            print(f"\n{f.get('contradiction', '')}")
            if f.get("user_impact"):
                print(f"\n**Impact**: {f['user_impact']}")
            print(f"\n_Source: {f.get('internal_evidence', '')}_\n")

    # Auto-record findings to JSONL
    try:
        from findings import add_finding, Finding, generate_id
        # Batch enrich all findings with git data
        try:
            from git_enrichment import enrich_findings_batch
            enrich_findings_batch(findings, repo_path, verbose=verbose)
        except Exception:
            pass
        repo_name = Path(repo_path).name
        recorded = 0
        for f in findings:
            loc = f.get("location", {})
            fid = generate_id(
                repo=repo_name,
                file=loc.get("file_a", ""),
                title=f.get("contradiction", "")[:80],
                file_b=loc.get("file_b", ""),
                pattern=f.get("pattern", ""),
            )
            finding = Finding(
                id=fid,
                repo=repo_name,
                file=loc.get("file_a", ""),
                type="contradiction",
                severity=f.get("severity", "medium"),
                pattern=f.get("pattern", ""),
                title=f.get("contradiction", "")[:120],
                description=f.get("contradiction", ""),
                status="found",
                found_by="deep_scan",
                category=f.get("category", ""),
                taxonomies=f.get("taxonomies"),
                churn_6m=f.get("churn_6m", 0),
                fan_out=f.get("fan_out", 0),
                total_lines=f.get("total_lines", 0),
            )
            try:
                add_finding(repo_path, finding)
                recorded += 1
            except ValueError:
                pass  # duplicate
        if verbose:
            print(f"  Recorded {recorded} findings to .delta-lint/findings/", file=sys.stderr)
    except Exception as e:
        if verbose:
            print(f"  Warning: could not record findings: {e}", file=sys.stderr)

    # Exit code 1 if high-severity findings exist
    high_count = sum(1 for f in findings if f.get("severity") == "high")
    if high_count > 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# cmd_scan_full (stress-test: virtual modifications × N → landmine map)
# ---------------------------------------------------------------------------

def cmd_scan_full(args):
    """Run full stress-test scan (heavy, 10-30 minutes)."""
    repo_path = str(Path(args.repo).resolve())

    print("── δ-lint ── フルスキャン（ストレステスト）開始", file=sys.stderr)
    print("  仮想改修を生成してスキャンします（10-30分）...", file=sys.stderr)

    from stress_test import run_stress_test
    run_stress_test(
        repo_path,
        backend=getattr(args, 'backend', 'cli'),
        verbose=getattr(args, 'verbose', False),
        lang=getattr(args, 'lang', 'en'),
    )

    # Convert high-risk files to debt findings
    from findings import ingest_stress_test_debt
    added = ingest_stress_test_debt(repo_path)
    if added:
        print(f"\n── δ-lint ── ストレステスト結果から {len(added)}件の技術的負債を登録", file=sys.stderr)


# ---------------------------------------------------------------------------
# cmd_watch (--watch mode)
# ---------------------------------------------------------------------------

def cmd_watch(args):
    """Watch mode: poll for file changes, re-scan on change."""
    import time
    import hashlib

    repo_path = str(Path(args.repo).resolve())
    repo_name = Path(repo_path).name
    interval = getattr(args, 'watch_interval', 3.0)

    def _get_file_snapshot():
        """Get hash of changed file list + mtimes for change detection."""
        try:
            changed = get_changed_files(repo_path, args.diff_target)
            source = filter_source_files(changed)
        except Exception:
            return None, []
        if not source:
            return None, []
        # Hash: file list + mtimes
        parts = []
        for f in sorted(source):
            full = Path(repo_path) / f
            try:
                parts.append(f"{f}:{full.stat().st_mtime_ns}")
            except OSError:
                parts.append(f"{f}:?")
        snap = hashlib.md5("|".join(parts).encode()).hexdigest()
        return snap, source

    print(f"── δ-lint ── Watch mode started", file=sys.stderr)
    print(f"  Repo: {repo_path}", file=sys.stderr)
    print(f"  Interval: {interval}s", file=sys.stderr)
    print(f"  Press Ctrl+C to stop\n", file=sys.stderr)

    last_snapshot = None
    scan_count = 0

    try:
        while True:
            snap, source_files = _get_file_snapshot()

            if snap is None:
                # No changed files — idle
                if last_snapshot is not None:
                    print(f"  ⏸ No changed files — waiting...", file=sys.stderr)
                    last_snapshot = None
                time.sleep(interval)
                continue

            if snap == last_snapshot:
                # No new changes since last scan
                time.sleep(interval)
                continue

            # Changes detected — run scan
            last_snapshot = snap
            scan_count += 1
            ts = time.strftime("%H:%M:%S")

            print(f"\n{'─' * 60}", file=sys.stderr)
            print(f"  🔍 [{ts}] Scan #{scan_count} — "
                  f"{len(source_files)} file(s) changed", file=sys.stderr)
            for f in source_files[:5]:
                print(f"    {f}", file=sys.stderr)
            if len(source_files) > 5:
                print(f"    ... +{len(source_files) - 5} more", file=sys.stderr)
            print(f"{'─' * 60}", file=sys.stderr)

            try:
                # Build context
                context = build_context(repo_path, source_files, retrieval_config=getattr(args, '_retrieval_config', None), doc_files=getattr(args, '_doc_files', None))
                if not context.target_files:
                    print(f"  ⚠ No readable source files. Skipping.", file=sys.stderr)
                    time.sleep(interval)
                    continue

                # Semantic expansion
                if args.semantic:
                    from semantic import expand_context_semantic
                    context = expand_context_semantic(
                        repo_path, source_files, context,
                        diff_target=args.diff_target,
                        verbose=args.verbose,
                    )

                # Load constraints & policy
                from detector import load_constraints, load_policy
                target_paths = [f.path for f in context.target_files]
                constraints = load_constraints(repo_path, target_paths)
                policy = load_policy(repo_path)
                architecture = policy.get("architecture") if policy else None

                # Diff context
                from retrieval import get_diff_content
                diff_text = get_diff_content(repo_path, args.diff_target)

                # Detect
                findings = detect(
                    context, repo_name=repo_name, model=args.model,
                    backend=args.backend, lang=args.lang,
                    constraints=constraints or None,
                    architecture=architecture,
                    diff_text=diff_text,
                )

                # Verify (Phase 2)
                if not getattr(args, 'no_verify', False) and findings:
                    from verifier import verify_findings as verify
                    findings, _, _ = verify(
                        findings, context,
                        model=args.model, backend=args.backend,
                        verbose=args.verbose,
                    )

                # Filter
                suppressions = load_suppressions(repo_path)
                result = filter_findings(
                    findings, min_severity=args.severity,
                    suppressions=suppressions, repo_path=repo_path,
                )

                # Policy filter
                if policy and (policy.get("accepted") or policy.get("severity_overrides")):
                    from findings import apply_policy
                    result.shown = apply_policy(result.shown, policy)

                # Output
                if result.shown:
                    print(f"\n  ⚡ {len(result.shown)} finding(s) detected:\n",
                          file=sys.stderr)
                    print_results(
                        result.shown,
                        filtered_count=len(result.filtered),
                        suppressed_count=len(result.suppressed),
                        expired_count=len(result.expired),
                        output_format=args.output_format,
                    )
                else:
                    print(f"\n  ✅ No issues found "
                          f"({len(findings)} raw → 0 after filter)\n",
                          file=sys.stderr)

                # Auto-learn sibling relationships
                if not getattr(args, 'no_learn', False) and findings:
                    try:
                        from sibling import update_sibling_map_from_findings
                        new_sibs = update_sibling_map_from_findings(findings, repo_path)
                        if new_sibs and args.verbose:
                            print(f"  Sibling map: +{new_sibs} new",
                                  file=sys.stderr)
                    except Exception:
                        pass

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"\n  ❌ Scan error: {e}", file=sys.stderr)

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\n── δ-lint ── Watch stopped ({scan_count} scan(s) completed)",
              file=sys.stderr)


# ---------------------------------------------------------------------------
# cmd_scan
# ---------------------------------------------------------------------------

def cmd_scan(args):
    """Run structural contradiction scan."""
    # Step 0: Environment pre-check (auto-install missing deps, never exits)
    env = _check_environment(
        backend=getattr(args, "backend", "cli"),
        verbose=getattr(args, "verbose", False),
    )
    # Apply resolved backend (may have changed if claude CLI unavailable)
    if hasattr(args, "backend"):
        args.backend = env["backend"]

    repo_path = str(Path(args.repo).resolve())
    repo_name = Path(repo_path).name

    # Step 1: Identify target files (driven by args._scope)
    scope = getattr(args, '_scope', 'diff')
    if args.files:
        source_files = args.files
        if args.verbose:
            print(f"Scanning {len(source_files)} specified file(s)", file=sys.stderr)
    elif scope == "all":
        # All source files in repo
        from surface_extractor import collect_all_source_files
        all_files = collect_all_source_files(repo_path)
        source_files = all_files if all_files else []
        if not source_files:
            print("No source files found in repository.", file=sys.stderr)
            sys.exit(0)
        if args.verbose:
            print(f"Scope=all: scanning all {len(source_files)} source file(s)", file=sys.stderr)
    elif scope == "smart" or getattr(args, 'smart', False):
        # Smart mode: select files by git history priority with batching
        # Run each batch as a separate subprocess with --files
        from retrieval import get_priority_batches
        batches = get_priority_batches(repo_path)
        if not batches:
            print("No priority files found from git history. Use --files to specify files manually.",
                  file=sys.stderr)
            sys.exit(0)
        total_files = sum(len(b) for b in batches)
        print(f"  🎯 Smart mode: git history から {total_files} ファイルを {len(batches)} バッチに分割", file=sys.stderr)
        for i, batch in enumerate(batches):
            print(f"    Batch {i+1}: {len(batch)} files", file=sys.stderr)
            if args.verbose:
                for f in batch:
                    print(f"      {f}", file=sys.stderr)

        # Run batches sequentially using --files (reuses existing scan logic)
        import subprocess
        script_path = Path(__file__).resolve()
        all_high = 0
        for i, batch in enumerate(batches):
            print(f"\n── Batch {i+1}/{len(batches)} ({len(batch)} files) ──", file=sys.stderr)
            cmd = [
                sys.executable, str(script_path), "scan",
                "--repo", repo_path,
                "--files", *batch,
                "--severity", getattr(args, 'severity', 'high'),
                "--lang", getattr(args, 'lang', 'en'),
            ]
            if getattr(args, 'verbose', False):
                cmd.append("--verbose")
            if getattr(args, 'no_cache', False):
                cmd.append("--no-cache")
            if getattr(args, 'no_verify', False):
                cmd.append("--no-verify")
            if getattr(args, 'autofix', False):
                cmd.append("--autofix")
            result = subprocess.run(cmd, cwd=repo_path)
            if result.returncode == 1:
                all_high += 1
        print(f"\n── Smart scan 完了: {len(batches)} バッチ実行 ──", file=sys.stderr)
        sys.exit(1 if all_high > 0 else 0)
    else:
        if args.verbose:
            print(f"Detecting changed files in {repo_path}...", file=sys.stderr)
        all_changed = get_changed_files(repo_path, args.diff_target)
        source_files = filter_source_files(all_changed)

        if not source_files:
            print("No changed source files found. Use --files or --smart to scan without changes.",
                  file=sys.stderr)
            sys.exit(0)

        if args.verbose:
            print(f"Found {len(source_files)} changed source file(s):", file=sys.stderr)
            for f in source_files:
                print(f"  {f}", file=sys.stderr)

    # Step 1.5: Resolve document files (--docs)
    if hasattr(args, "docs") and args.docs is not None:
        if args.docs:  # explicit paths given
            args._doc_files = args.docs
        else:  # --docs with no arguments → auto-discover
            args._doc_files = _auto_discover_docs(repo_path)
        if args.verbose and getattr(args, '_doc_files', None):
            print(f"Document contract surfaces: {len(args._doc_files)}", file=sys.stderr)
            for d in args._doc_files:
                print(f"  {d}", file=sys.stderr)

    # Step 2: Build context
    if args.verbose:
        print(f"Building module context...", file=sys.stderr)

    context = build_context(repo_path, source_files, retrieval_config=getattr(args, '_retrieval_config', None), doc_files=getattr(args, '_doc_files', None))

    if args.verbose:
        print(f"  Target files: {len(context.target_files)}", file=sys.stderr)
        print(f"  Dependency files: {len(context.dep_files)}", file=sys.stderr)
        if context.doc_files:
            print(f"  Document files: {len(context.doc_files)}", file=sys.stderr)
        print(f"  Total context: {context.total_chars} chars", file=sys.stderr)
        for w in context.warnings:
            print(f"  WARNING: {w}", file=sys.stderr)

    if not context.target_files:
        print("No readable source files in context. Nothing to scan.", file=sys.stderr)
        sys.exit(0)

    # Step 2.5: Semantic expansion (--semantic)
    if args.semantic:
        from semantic import expand_context_semantic
        context = expand_context_semantic(
            repo_path, source_files, context,
            diff_target=args.diff_target,
            verbose=args.verbose,
        )

    # Step 3: Dry run - show context and exit
    if args.dry_run:
        print("=== DRY RUN: Context that would be sent to LLM ===\n", file=sys.stderr)
        print(f"Target files ({len(context.target_files)}):", file=sys.stderr)
        for f in context.target_files:
            print(f"  {f.path} ({len(f.content)} chars)", file=sys.stderr)
        print(f"Dependency files ({len(context.dep_files)}):", file=sys.stderr)
        for f in context.dep_files:
            print(f"  {f.path} ({len(f.content)} chars)", file=sys.stderr)
        print(f"\nTotal: {context.total_chars} chars", file=sys.stderr)
        if context.warnings:
            print(f"\nWarnings:", file=sys.stderr)
            for w in context.warnings:
                print(f"  {w}", file=sys.stderr)
        sys.exit(0)

    # Step 3.5: Load known constraints, team policy, and config
    from detector import load_constraints, load_policy
    config = _load_config(repo_path)
    target_paths = [f.path for f in context.target_files]
    constraints = load_constraints(repo_path, target_paths)
    policy = load_policy(repo_path)

    # Merge profile policy into constraints.yml policy (profile wins on conflict)
    profile_policy = getattr(args, '_profile_policy', None)
    if profile_policy:
        if not policy:
            policy = {}
        # prompt_append: concatenate (profile appends to constraints.yml)
        if "prompt_append" in profile_policy:
            existing = policy.get("prompt_append", "")
            policy["prompt_append"] = (existing + "\n\n" + profile_policy["prompt_append"]).strip()
        # disabled_patterns: profile overrides config.json
        if "disabled_patterns" in profile_policy:
            config["disabled_patterns"] = profile_policy["disabled_patterns"]
        # detect_prompt: custom detection prompt path or inline
        # - File path (relative to repo): loaded and used as system prompt
        # - Inline string: used directly as system prompt
        if "detect_prompt" in profile_policy:
            policy["detect_prompt"] = profile_policy["detect_prompt"]
        # accepted: accepted rules override (per-pattern or per-file exceptions)
        if "accepted" in profile_policy:
            policy["accepted"] = profile_policy["accepted"]
        # severity_overrides: per-pattern severity remapping
        if "severity_overrides" in profile_policy:
            policy["severity_overrides"] = profile_policy["severity_overrides"]
        # debt_budget: max active debt score threshold for CI gate
        if "debt_budget" in profile_policy:
            policy["debt_budget"] = profile_policy["debt_budget"]
        # scoring_weights: override scoring formula weights
        if "scoring_weights" in profile_policy:
            policy["scoring_weights"] = profile_policy["scoring_weights"]
        # dashboard_template: custom findings dashboard HTML template
        if "dashboard_template" in profile_policy:
            args._dashboard_template = profile_policy["dashboard_template"]
        # docs: enable document contract surface checking from profile
        if "docs" in profile_policy and not getattr(args, '_doc_files', None):
            doc_val = profile_policy["docs"]
            if doc_val is True:
                args._doc_files = _auto_discover_docs(repo_path)
            elif isinstance(doc_val, list):
                args._doc_files = doc_val
        # Other policy keys: profile overrides
        for k in ("architecture", "project_rules", "exclude_paths"):
            if k in profile_policy:
                policy[k] = profile_policy[k]
        if args.verbose:
            print(f"  Profile policy merged: {list(profile_policy.keys())}", file=sys.stderr)

    if constraints and args.verbose:
        total_c = sum(len(c.get("implicit_constraints", [])) for c in constraints)
        print(f"  Loaded {total_c} constraint(s) from {len(constraints)} module(s)", file=sys.stderr)
    # Apply exclude_paths from policy (filter out 3rd-party / vendor code)
    exclude_paths = policy.get("exclude_paths", []) if policy else []
    if exclude_paths:
        import fnmatch
        before_count = len(source_files)
        source_files = [
            f for f in source_files
            if not any(fnmatch.fnmatch(f, pat) for pat in exclude_paths)
        ]
        excluded_count = before_count - len(source_files)
        if excluded_count > 0:
            # Rebuild context without excluded files
            context = build_context(repo_path, source_files, retrieval_config=getattr(args, '_retrieval_config', None), doc_files=getattr(args, '_doc_files', None))
            if args.verbose:
                print(f"  Excluded {excluded_count} file(s) by policy exclude_paths", file=sys.stderr)

    if policy and args.verbose:
        parts = []
        if policy.get("architecture"):
            parts.append(f"{len(policy['architecture'])} architecture context(s)")
        if policy.get("project_rules"):
            parts.append(f"{len(policy['project_rules'])} project rule(s)")
        if policy.get("exclude_paths"):
            parts.append(f"{len(policy['exclude_paths'])} exclude path(s)")
        if policy.get("accepted"):
            parts.append(f"{len(policy['accepted'])} accepted rule(s)")
        if policy.get("severity_overrides"):
            parts.append(f"{len(policy['severity_overrides'])} severity override(s)")
        if policy.get("prompt_append"):
            parts.append("prompt_append")
        if policy.get("debt_budget") is not None:
            parts.append(f"debt_budget={policy['debt_budget']}")
        if parts:
            print(f"  Policy: {', '.join(parts)}", file=sys.stderr)

    if config.get("disabled_patterns") and args.verbose:
        print(f"  Disabled patterns: {', '.join(config['disabled_patterns'])}", file=sys.stderr)

    # Step 3.7: Get git diff for change-aware detection
    from retrieval import get_diff_content
    diff_text = ""
    if not args.files:
        # Only include diff when using git-based file detection (not --files)
        diff_text = get_diff_content(repo_path, args.diff_target)
        if args.verbose and diff_text:
            print(f"  Diff context: {len(diff_text)} chars", file=sys.stderr)

    # Step 3.9: Check cache (skip LLM if same context was scanned before)
    from cache import compute_context_hash, get_cached_findings, save_cached_findings
    context_hash = compute_context_hash(context.target_files, context.dep_files)
    cache_hit = False

    if not getattr(args, 'no_cache', False):
        cached = get_cached_findings(repo_path, context_hash)
        if cached is not None:
            findings = cached
            cache_hit = True
            if args.verbose:
                print(f"  Cache hit ({context_hash}) — {len(findings)} finding(s)",
                      file=sys.stderr)

    # Step 4: Run detection (skip if cache hit)
    if not cache_hit:
        if args.verbose:
            print(f"Running detection with {args.model}...", file=sys.stderr)

        architecture = policy.get("architecture") if policy else None
        project_rules = policy.get("project_rules") if policy else None
        prompt_append = policy.get("prompt_append", "") if policy else ""
        disabled_patterns = config.get("disabled_patterns") if config else None

        # detect_prompt: profile can override the entire detection prompt
        # Value can be a file path (relative to repo) or inline prompt text
        detect_prompt_override = ""
        raw_detect_prompt = policy.get("detect_prompt", "") if policy else ""
        if raw_detect_prompt:
            # If it looks like a file path, try to load it
            prompt_file = Path(repo_path) / raw_detect_prompt
            if prompt_file.exists() and prompt_file.is_file():
                detect_prompt_override = prompt_file.read_text(encoding="utf-8")
                if args.verbose:
                    print(f"  Custom detect prompt: {raw_detect_prompt}", file=sys.stderr)
            else:
                # Treat as inline prompt text
                detect_prompt_override = raw_detect_prompt
                if args.verbose:
                    print(f"  Custom detect prompt: inline ({len(raw_detect_prompt)} chars)", file=sys.stderr)

        findings = detect(context, repo_name=repo_name, model=args.model,
                           backend=args.backend, lang=args.lang,
                           constraints=constraints or None,
                           architecture=architecture,
                           diff_text=diff_text,
                           project_rules=project_rules,
                           repo_path=repo_path,
                           prompt_append=prompt_append,
                           disabled_patterns=disabled_patterns,
                           detect_prompt=detect_prompt_override,
                           lens=getattr(args, '_lens', 'default'))

        if args.verbose:
            print(f"  Raw findings: {len(findings)}", file=sys.stderr)

    # Step 4.2: Verify findings (Phase 2 — reject false positives)
    verification_meta = None
    rejected_findings = []
    if not getattr(args, 'no_verify', False) and findings:
        from verifier import verify_findings as verify
        findings, rejected_findings, verification_meta = verify(
            findings, context,
            model=args.model, backend=args.backend,
            verbose=args.verbose,
        )
        if args.verbose and verification_meta:
            print(f"  After verification: {verification_meta['confirmed']} confirmed, "
                  f"{verification_meta['rejected']} rejected", file=sys.stderr)

    # Step 4.3: Save to cache (after verification, so cache includes verified results)
    if not cache_hit and not getattr(args, 'no_cache', False):
        save_cached_findings(repo_path, context_hash, findings, model=args.model)
        if args.verbose:
            print(f"  Cached results ({context_hash})", file=sys.stderr)

    # Step 4.5: Load suppressions
    suppressions = load_suppressions(repo_path)
    if args.verbose and suppressions:
        print(f"  Loaded {len(suppressions)} suppress entry(ies)", file=sys.stderr)

    # Step 5: Filter and output (with suppress support)
    result = filter_findings(findings, min_severity=args.severity,
                             suppressions=suppressions, repo_path=repo_path)

    # Step 5.1: Apply team policy (accepted filter + severity overrides)
    policy_filtered = 0
    if policy and (policy.get("accepted") or policy.get("severity_overrides")):
        from findings import apply_policy
        before = len(result.shown)
        result.shown = apply_policy(result.shown, policy)
        policy_filtered = before - len(result.shown)

    # Step 5.15: Apply category severity boost
    config = _load_config(repo_path)
    categories = config.get("categories", {})
    if categories:
        _apply_category_severity_boost(result.shown, categories, verbose=args.verbose)
        # Re-filter: boosted findings may now fall below min_severity
        from output import SEVERITY_ORDER
        threshold = SEVERITY_ORDER.get(args.severity, 0)
        before_cat = len(result.shown)
        result.shown = [f for f in result.shown
                        if SEVERITY_ORDER.get(f.get("severity", "medium").lower(), 1) <= threshold]
        cat_filtered = before_cat - len(result.shown)
        if cat_filtered and args.verbose:
            print(f"  Category filtered: {cat_filtered}", file=sys.stderr)

    # Step 5.2: diff-only filter (keep only findings touching changed files)
    diff_only_filtered = 0
    if getattr(args, 'diff_only', False) and not args.files:
        from output import filter_diff_only
        before = len(result.shown)
        result.shown = filter_diff_only(result.shown, source_files)
        diff_only_filtered = before - len(result.shown)

    if args.verbose:
        print(f"  Shown (>= {args.severity}): {len(result.shown)}", file=sys.stderr)
        print(f"  Filtered: {len(result.filtered)}", file=sys.stderr)
        if diff_only_filtered:
            print(f"  Diff-only filtered: {diff_only_filtered}", file=sys.stderr)
        if policy_filtered:
            print(f"  Policy accepted: {policy_filtered}", file=sys.stderr)
        if result.suppressed:
            print(f"  Suppressed: {len(result.suppressed)}", file=sys.stderr)
        if result.expired:
            print(f"  Expired: {len(result.expired)}", file=sys.stderr)

    # Report expired suppressions as warnings
    for entry in result.expired_entries:
        files_str = " <-> ".join(entry.files)
        print(f"WARNING: suppress {entry.id} expired (code changed): {files_str}",
              file=sys.stderr)

    # Resolve persona: explicit --for > config.json > "engineer"
    persona = args.persona
    if persona is None:
        from persona_translator import load_default_persona
        persona = load_default_persona(repo_path)

    if persona in ("pm", "qa"):
        # Non-engineer mode: translate findings instead of technical output
        from persona_translator import translate
        translated = translate(result.shown, persona=persona,
                               model=args.model, verbose=args.verbose)
        if translated:
            print(translated)
        else:
            # Fallback: show standard output if translation failed
            print_results(result.shown,
                          filtered_count=len(result.filtered),
                          suppressed_count=len(result.suppressed),
                          expired_count=len(result.expired),
                          output_format=args.output_format)
    else:
        print_results(result.shown,
                      filtered_count=len(result.filtered),
                      suppressed_count=len(result.suppressed),
                      expired_count=len(result.expired),
                      output_format=args.output_format)

    # Step 5.5: Autofix (generate + apply locally)
    if getattr(args, 'autofix', False) and result.shown:
        from fixgen import generate_fixes, apply_fixes_locally
        print(f"\n── Autofix: generating fixes for {len(result.shown)} finding(s)...",
              file=sys.stderr)
        fixes = generate_fixes(
            result.shown, context,
            model=args.model, backend=args.backend,
            verbose=args.verbose,
        )
        if fixes:
            applied = apply_fixes_locally(fixes, repo_path, verbose=args.verbose)
            if applied:
                print(f"\n✅ Applied {len(applied)} fix(es):", file=sys.stderr)
                for fix in applied:
                    explanation = fix.get("explanation", "")
                    print(f"  {fix.get('file', '?')}:{fix.get('line', '?')} — {explanation}",
                          file=sys.stderr)
            else:
                print("\n⚠ Fixes generated but none could be applied (old_code mismatch).",
                      file=sys.stderr)
        else:
            print("\n⚠ No fixes could be generated.", file=sys.stderr)

    # Step 6: Save log
    log_dir = args.log_dir or str(Path(repo_path) / ".delta-lint")
    context_meta = {
        "repo": repo_name,
        "repo_path": repo_path,
        "target_files": [f.path for f in context.target_files],
        "dep_files": [f.path for f in context.dep_files],
        "total_chars": context.total_chars,
        "model": args.model,
        "severity_filter": args.severity,
        "warnings": context.warnings,
    }
    if verification_meta:
        context_meta["verification"] = verification_meta
    if rejected_findings:
        context_meta["rejected_findings"] = rejected_findings
    log_path = save_log(result, context_meta, log_dir)
    if args.verbose:
        print(f"\nFull log saved to {log_path}", file=sys.stderr)

    # Step 6.3: Record scan history (with finding_ids for Chao1 coverage estimation)
    try:
        from findings import append_scan_history, generate_id
        scan_fids = []
        scan_patterns = []
        for f in result.shown:
            loc = f.get("location", {})
            fid = generate_id(
                repo_name, loc.get("file_a", ""),
                f.get("contradiction", f.get("title", ""))[:120],
                file_b=loc.get("file_b", ""), pattern=f.get("pattern", ""),
            )
            scan_fids.append(fid)
            if f.get("pattern"):
                scan_patterns.append(f["pattern"])
        # Resolve scan_type from 3-axis model for backward compat
        _scan_type = "diff"
        if scope == "smart":
            _scan_type = "existing"
        elif scope == "all":
            _scan_type = "deep" if getattr(args, '_depth', '1hop') == "graph" else "existing"
        append_scan_history(
            repo_path,
            clusters=len(context.target_files),
            findings_count=len(result.shown),
            scan_type=_scan_type,
            finding_ids=scan_fids,
            patterns_found=scan_patterns,
            scope=scope,
            depth=getattr(args, '_depth', '1hop'),
            lens=getattr(args, '_lens', 'default'),
        )
    except Exception:
        pass

    # Step 6.5: Update sibling map (auto-learn from findings)
    if not getattr(args, 'no_learn', False) and findings:
        try:
            from sibling import update_sibling_map_from_findings
            new_siblings = update_sibling_map_from_findings(findings, repo_path)
            if new_siblings and args.verbose:
                print(f"  Sibling map: +{new_siblings} new relationship(s)", file=sys.stderr)
        except Exception:
            pass  # Non-critical — don't fail scan for sibling map errors

    # Step 6.7: Save baseline snapshot (--baseline-save)
    if getattr(args, 'baseline_save', False):
        all_findings = result.shown + result.filtered + result.suppressed
        snap = _save_baseline_snapshot(repo_path, all_findings, verbose=args.verbose)
        if snap:
            print(f"\n✅ Baseline snapshot saved: {snap}", file=sys.stderr)

    # Step 7: Baseline comparison (--baseline)
    baseline_ref = getattr(args, 'baseline', None)
    if baseline_ref and result.shown:
        baseline_hashes = _build_baseline_hashes(repo_path, baseline_ref, verbose=args.verbose)
        if baseline_hashes is not None:
            new_findings, baseline_count = _filter_new_findings(result.shown, baseline_hashes)
            if args.verbose or baseline_count > 0:
                print(f"\n  Baseline comparison vs {baseline_ref}:", file=sys.stderr)
                print(f"    Known (in baseline): {baseline_count}", file=sys.stderr)
                print(f"    New: {len(new_findings)}", file=sys.stderr)
            # Only new findings determine exit code
            high_count = sum(1 for f in new_findings if f.get("severity", "").lower() == "high")
            if new_findings:
                print(f"\n⚠ {len(new_findings)} new finding(s) since {baseline_ref}"
                      f" ({high_count} high)", file=sys.stderr)
            else:
                print(f"\n✅ No new findings since {baseline_ref}"
                      f" ({baseline_count} existing, all known)", file=sys.stderr)
            sys.exit(1 if high_count > 0 else 0)
        else:
            if args.verbose:
                print(f"  No baseline snapshot found for {baseline_ref}. "
                      f"Falling back to normal exit logic.", file=sys.stderr)

    # Exit code: 1 if high-severity findings or debt_budget exceeded
    high_count = sum(1 for f in result.shown if f.get("severity", "").lower() == "high")

    # debt_budget gate (CI integration)
    debt_budget = policy.get("debt_budget") if policy else None
    if debt_budget is not None:
        from findings import finding_debt_score
        from scoring import load_scoring_config
        _scoring_overrides = policy.get("scoring_weights") if policy else None
        scoring_cfg = load_scoring_config(repo_path, profile_overrides=_scoring_overrides)
        active_debt = sum(finding_debt_score(f, scoring_cfg) for f in result.shown)
        if active_debt > debt_budget:
            print(f"\n⚠ Debt budget exceeded: {active_debt:.1f} > {debt_budget} (budget)",
                  file=sys.stderr)
            sys.exit(1)
        elif args.verbose:
            print(f"  Debt budget OK: {active_debt:.1f} <= {debt_budget}", file=sys.stderr)

    sys.exit(1 if high_count > 0 else 0)


# ---------------------------------------------------------------------------
# cmd_suppress
# ---------------------------------------------------------------------------

def cmd_view(args):
    """Open unified delta-lint dashboard in the browser."""
    repo_path = Path(args.repo).resolve()
    delta_dir = repo_path / ".delta-lint"

    if not delta_dir.exists():
        print("⚠ .delta-lint/ が見つかりません。", file=sys.stderr)
        print("  先に delta init を実行してください。", file=sys.stderr)
        sys.exit(1)

    dash_path = delta_dir / "findings" / "dashboard.html"
    regenerate = getattr(args, "regenerate", False)

    if dash_path.exists() and not regenerate:
        import subprocess as _sp
        _sp.Popen(["open", str(dash_path)])
        print(f"✓ ダッシュボード: {dash_path}")
        return

    # Regenerate: build treemap JSON if results.json exists, then generate unified dashboard
    from findings import generate_dashboard

    treemap_json = None
    results_path = delta_dir / "stress-test" / "results.json"
    if results_path.exists():
        from visualize import build_treemap_json
        treemap_json = build_treemap_json(str(results_path))

    has_findings = any((delta_dir / "findings").glob("*.jsonl")) if (delta_dir / "findings").exists() else False
    if not has_findings and treemap_json is None:
        print("⚠ データがありません。delta scan を実行してください。", file=sys.stderr)
        sys.exit(1)

    _dash_tpl = getattr(args, '_dashboard_template', "")
    out = generate_dashboard(str(repo_path), treemap_json=treemap_json, dashboard_template=_dash_tpl)
    import subprocess as _sp
    _sp.Popen(["open", str(out)])
    print(f"✓ ダッシュボード (再生成): {out}")


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


def _normalize_scan_axes(args):
    """Normalize legacy flags (--smart, --deep, --full) to 3-axis model.

    Sets args._scope, args._depth, args._lens as resolved values.
    Explicit --scope/--depth/--lens always take priority over legacy flags.
    """
    # Scope: --scope > --smart > default(diff)
    if args.scope is not None:
        args._scope = args.scope
    elif getattr(args, 'smart', False):
        args._scope = "smart"
    elif getattr(args, 'files', None):
        args._scope = "files"  # explicit file list
    else:
        args._scope = "diff"

    # Depth: --depth > --deep > default(1hop)
    if args.depth is not None:
        args._depth = args.depth
    elif getattr(args, 'deep', False):
        args._depth = "graph"
    else:
        args._depth = "1hop"

    # Lens: --lens > --full > default
    if args.lens is not None:
        args._lens = args.lens
    elif getattr(args, 'full', False):
        args._lens = "stress"
    else:
        args._lens = "default"

    if getattr(args, 'verbose', False):
        print(f"  Scan axes: scope={args._scope} depth={args._depth} lens={args._lens}",
              file=sys.stderr)


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
        choices=["diff", "smart", "all"],
        help="Scan scope: diff (changed files, default), smart (git history priority), "
             "all (entire codebase). Replaces --smart flag.",
    )
    scan_parser.add_argument(
        "--depth", default=None,
        choices=["1hop", "graph"],
        help="Context depth: 1hop (import-based 1-hop deps, default), "
             "graph (contract graph analysis via surface extraction).",
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
        help=argparse.SUPPRESS,  # hidden: use --depth graph
    )
    scan_parser.add_argument(
        "--deep-workers", type=int, default=4,
        help="Number of parallel LLM verification workers for deep scan (default: 4)",
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
        elif args._depth == "graph":
            cmd_scan_deep(args)
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
