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

def _load_config(repo_path: str = ".") -> dict:
    """Load .delta-lint/config.json if it exists. Returns empty dict otherwise."""
    config_path = Path(repo_path).resolve() / ".delta-lint" / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _apply_config_to_parser(parser, config: dict):
    """Override parser defaults with config values. CLI flags still win."""
    mapping = {
        "lang": "lang",
        "backend": "backend",
        "severity": "severity",
        "model": "model",
        "verbose": "verbose",
        "semantic": "semantic",
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

    # Step 1: Identify target files
    if args.files:
        source_files = args.files
        if args.verbose:
            print(f"Scanning {len(source_files)} specified file(s)", file=sys.stderr)
    else:
        if args.verbose:
            print(f"Detecting changed files in {repo_path}...", file=sys.stderr)
        all_changed = get_changed_files(repo_path, args.diff_target)
        source_files = filter_source_files(all_changed)

        if not source_files:
            print("No changed source files found. Use --files to specify files manually.",
                  file=sys.stderr)
            sys.exit(0)

        if args.verbose:
            print(f"Found {len(source_files)} changed source file(s):", file=sys.stderr)
            for f in source_files:
                print(f"  {f}", file=sys.stderr)

    # Step 2: Build context
    if args.verbose:
        print(f"Building module context...", file=sys.stderr)

    context = build_context(repo_path, source_files)

    if args.verbose:
        print(f"  Target files: {len(context.target_files)}", file=sys.stderr)
        print(f"  Dependency files: {len(context.dep_files)}", file=sys.stderr)
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

    # Step 4: Run detection
    if args.verbose:
        print(f"Running detection with {args.model}...", file=sys.stderr)

    findings = detect(context, repo_name=repo_name, model=args.model,
                       backend=args.backend, lang=args.lang)

    if args.verbose:
        print(f"  Raw findings: {len(findings)}", file=sys.stderr)

    # Step 4.5: Load suppressions
    suppressions = load_suppressions(repo_path)
    if args.verbose and suppressions:
        print(f"  Loaded {len(suppressions)} suppress entry(ies)", file=sys.stderr)

    # Step 5: Filter and output (with suppress support)
    result = filter_findings(findings, min_severity=args.severity,
                             suppressions=suppressions, repo_path=repo_path)

    if args.verbose:
        print(f"  Shown (>= {args.severity}): {len(result.shown)}", file=sys.stderr)
        print(f"  Filtered: {len(result.filtered)}", file=sys.stderr)
        if result.suppressed:
            print(f"  Suppressed: {len(result.suppressed)}", file=sys.stderr)
        if result.expired:
            print(f"  Expired: {len(result.expired)}", file=sys.stderr)

    # Report expired suppressions as warnings
    for entry in result.expired_entries:
        files_str = " <-> ".join(entry.files)
        print(f"WARNING: suppress {entry.id} expired (code changed): {files_str}",
              file=sys.stderr)

    print_results(result.shown,
                  filtered_count=len(result.filtered),
                  suppressed_count=len(result.suppressed),
                  expired_count=len(result.expired),
                  output_format=args.output_format)

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
    log_path = save_log(result, context_meta, log_dir)
    if args.verbose:
        print(f"\nFull log saved to {log_path}", file=sys.stderr)

    # Exit code: 1 if high-severity findings, 0 otherwise
    high_count = sum(1 for f in result.shown if f.get("severity", "").lower() == "high")
    sys.exit(1 if high_count > 0 else 0)


# ---------------------------------------------------------------------------
# cmd_suppress
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
        print(f"  [{e.id}] Pattern {e.pattern} — {files_str}")
        print(f"    why_type: {e.why_type}")
        print(f"    why: {e.why}")
        print(f"    date: {e.date}, author: {e.author}")
        if e.line_ranges:
            print(f"    lines: {', '.join(e.line_ranges)}")
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

    # Load config.json and apply as parser defaults (CLI flags still win)
    # Pre-scan argv for --repo to find the right .delta-lint/config.json
    _repo_hint = "."
    for i, arg in enumerate(sys.argv):
        if arg == "--repo" and i + 1 < len(sys.argv):
            _repo_hint = sys.argv[i + 1]
            break
    _config = _load_config(_repo_hint)
    if _config:
        _apply_config_to_parser(scan_parser, _config)

    args = parser.parse_args()

    # Default to scan when no subcommand given (backward compat)
    if args.command is None:
        # Re-parse as scan
        scan_parser.parse_args(sys.argv[1:], namespace=args)
        args.command = "scan"

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "suppress":
        cmd_suppress(args)
    elif args.command == "findings":
        cmd_findings(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
