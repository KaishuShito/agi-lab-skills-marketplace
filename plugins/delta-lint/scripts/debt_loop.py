"""
debt_loop.py — Automated debt resolution loop.

Picks top N findings by priority, creates one branch + PR per finding.
Each finding gets its own branch and minimal fix PR.

Usage:
    python debt_loop.py --repo /path/to/repo --count 3
    python debt_loop.py --repo /path/to/repo --ids kingsman-a1b2c3d4,kingsman-e5f6g7h8
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Ensure scripts/ is in path for sibling imports
sys.path.insert(0, str(Path(__file__).parent))

from findings import list_findings, load_scan_history, update_status
from info_theory import finding_information_score
from fixgen import generate_fixes, apply_fixes_locally


# ---------------------------------------------------------------------------
# Lightweight context for fixgen — reads source files from finding location
# ---------------------------------------------------------------------------

class FindingContext:
    """Minimal context for fixgen.generate_fixes().

    Reads the files referenced in a finding so the LLM has source code.
    """

    def __init__(self, finding: dict, repo_path: str):
        self.repo_path = Path(repo_path)
        self.finding = finding
        self._files: dict[str, str] = {}
        self._load_files()

    def _load_files(self):
        """Load source files referenced in the finding."""
        loc = self.finding.get("location", {})
        file_a = loc.get("file_a", self.finding.get("file", ""))
        file_b = loc.get("file_b", "")

        for fpath in [file_a, file_b]:
            if not fpath:
                continue
            full = self.repo_path / fpath
            if full.exists():
                try:
                    self._files[fpath] = full.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    def to_prompt_string(self) -> str:
        """Format source files for LLM prompt."""
        parts = []
        for fpath, content in self._files.items():
            parts.append(f"### {fpath}\n```\n{content}\n```")
        return "\n\n".join(parts) if parts else "(source files not available)"


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

def score_finding(
    f: dict,
    scan_history: list[dict] | None = None,
    all_findings: list[dict] | None = None,
) -> float:
    """Compute priority score for a finding (higher = fix first)."""
    try:
        pool = all_findings if all_findings is not None else [f]
        info = finding_information_score(f, scan_history, all_findings=pool)
        info_score = info["info_score"]
    except Exception:
        info_score = 0

    # Compute ROI if not already present
    roi = f.get("roi_score")
    if roi is None:
        try:
            from scoring import compute_roi
            roi_data = compute_roi(
                severity=f.get("severity", "low"),
                churn_6m=f.get("churn_6m", 0),
                fan_out=f.get("fan_out", 0),
                pattern=f.get("pattern", ""),
                fix_churn_6m=f.get("fix_churn_6m"),
            )
            roi = roi_data["roi_score"]
            f["roi_score"] = roi
        except Exception:
            roi = 0

    sev_bonus = {"high": 300, "medium": 100, "low": 0}.get(f.get("severity", "low"), 0)

    return info_score + (roi or 0) + sev_bonus


# ---------------------------------------------------------------------------
# Git + PR operations
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command."""
    return subprocess.run(
        ["git"] + args,
        cwd=cwd, capture_output=True, text=True,
        check=check,
    )


def _current_branch(repo_path: str) -> str:
    """Get current branch name."""
    r = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
    return r.stdout.strip()


def _branch_exists(repo_path: str, branch: str) -> bool:
    """Check if branch exists locally or remotely."""
    r = _run_git(["branch", "--list", branch], repo_path, check=False)
    if r.stdout.strip():
        return True
    r = _run_git(["ls-remote", "--heads", "origin", branch], repo_path, check=False)
    return bool(r.stdout.strip())


def process_one_finding(
    finding: dict,
    repo_path: str,
    base_branch: str,
    model: str,
    backend: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict | None:
    """Process a single finding: branch → fix → commit → PR.

    Returns result dict or None if fix failed.
    """
    fid = finding.get("id", "unknown")
    pattern = finding.get("pattern", "")
    title = finding.get("title", finding.get("contradiction", ""))
    short_title = title[:60].strip()

    branch = f"debt-loop/{fid}"

    if verbose:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  Finding: {fid} ({pattern})", file=sys.stderr)
        print(f"  Title: {short_title}", file=sys.stderr)
        print(f"  Branch: {branch}", file=sys.stderr)

    # Check if branch already exists (already being worked on)
    if _branch_exists(repo_path, branch):
        if verbose:
            print(f"  SKIP: branch {branch} already exists", file=sys.stderr)
        return {"finding_id": fid, "status": "skipped", "reason": "branch_exists"}

    # Return to base branch
    _run_git(["checkout", base_branch], repo_path)

    # Create feature branch
    _run_git(["checkout", "-b", branch], repo_path)

    try:
        # Build context from finding's source files
        context = FindingContext(finding, repo_path)

        # Generate fix
        if verbose:
            print(f"  Generating fix...", file=sys.stderr)

        fixes = generate_fixes(
            [finding], context,
            model=model, backend=backend, verbose=verbose,
        )

        if not fixes:
            if verbose:
                print(f"  No fix generated", file=sys.stderr)
            _run_git(["checkout", base_branch], repo_path)
            _run_git(["branch", "-D", branch], repo_path, check=False)
            return {"finding_id": fid, "status": "no_fix"}

        # Apply fixes locally
        applied = apply_fixes_locally(fixes, repo_path, verbose=verbose)

        if not applied:
            if verbose:
                print(f"  Fix could not be applied", file=sys.stderr)
            _run_git(["checkout", base_branch], repo_path)
            _run_git(["branch", "-D", branch], repo_path, check=False)
            return {"finding_id": fid, "status": "apply_failed"}

        if dry_run:
            if verbose:
                print(f"  DRY RUN: {len(applied)} fix(es) would be applied", file=sys.stderr)
            _run_git(["checkout", ".", "--"], repo_path, check=False)
            _run_git(["checkout", base_branch], repo_path)
            _run_git(["branch", "-D", branch], repo_path, check=False)
            return {"finding_id": fid, "status": "dry_run", "fixes": len(applied)}

        # Stage changed files
        changed_files = [f["file"] for f in applied]
        _run_git(["add"] + changed_files, repo_path)

        # Commit
        commit_msg = f"fix: {short_title}\n\nResolves delta-lint finding {fid} (pattern {pattern})"
        _run_git(["commit", "-m", commit_msg], repo_path)

        # Push
        _run_git(["push", "-u", "origin", branch], repo_path)

        # Create PR
        pr_body = _build_pr_body(finding, applied)
        pr_result = subprocess.run(
            ["gh", "pr", "create",
             "--title", f"fix: {short_title}",
             "--body", pr_body,
             "--base", base_branch],
            cwd=repo_path, capture_output=True, text=True,
        )

        pr_url = pr_result.stdout.strip() if pr_result.returncode == 0 else None

        if verbose:
            if pr_url:
                print(f"  PR created: {pr_url}", file=sys.stderr)
            else:
                print(f"  PR creation failed: {pr_result.stderr[:200]}", file=sys.stderr)

        # Return to base
        _run_git(["checkout", base_branch], repo_path)

        return {
            "finding_id": fid,
            "status": "pr_created" if pr_url else "pushed",
            "branch": branch,
            "pr_url": pr_url,
            "fixes": len(applied),
        }

    except Exception as e:
        if verbose:
            print(f"  ERROR: {e}", file=sys.stderr)
        # Cleanup: return to base branch
        _run_git(["checkout", base_branch], repo_path, check=False)
        _run_git(["branch", "-D", branch], repo_path, check=False)
        return {"finding_id": fid, "status": "error", "error": str(e)}


def _build_pr_body(finding: dict, applied: list[dict]) -> str:
    """Build PR description from finding and applied fixes."""
    lines = ["## Summary", ""]
    lines.append(f"Automated fix for structural contradiction detected by delta-lint.")
    lines.append("")
    lines.append(f"- **Finding**: `{finding.get('id', '')}`")
    lines.append(f"- **Pattern**: {finding.get('pattern', '')} {finding.get('mechanism', '')}")
    lines.append(f"- **Severity**: {finding.get('severity', 'unknown')}")
    lines.append("")
    lines.append("### Contradiction")
    lines.append(finding.get("contradiction", "N/A"))
    lines.append("")
    lines.append("### Impact")
    lines.append(finding.get("impact", "N/A"))
    lines.append("")
    lines.append("### Changes")
    for f in applied:
        lines.append(f"- `{f.get('file', '')}`: {f.get('explanation', '')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_debt_loop(
    repo_path: str,
    count: int = 3,
    finding_ids: list[str] | None = None,
    model: str = "claude-sonnet-4-20250514",
    backend: str = "cli",
    base_branch: str | None = None,
    status_filter: str = "found,verified",
    dry_run: bool = False,
    verbose: bool = False,
) -> list[dict]:
    """Run the debt resolution loop.

    Args:
        repo_path: Path to git repository
        count: Max number of findings to process
        finding_ids: Specific finding IDs to fix (overrides priority sort)
        model: LLM model for fix generation
        backend: "cli" ($0) or "api" (pay-per-use)
        base_branch: Branch to create fix branches from (default: current)
        status_filter: Comma-separated statuses to include
        dry_run: Generate fixes but don't commit/push/PR
        verbose: Print progress

    Returns:
        List of result dicts per finding
    """
    repo_path = str(Path(repo_path).resolve())

    if base_branch is None:
        base_branch = _current_branch(repo_path)

    # Check for uncommitted changes (ignore untracked files)
    status = _run_git(["status", "--porcelain", "-uno"], repo_path)
    if status.stdout.strip():
        print("ERROR: Working directory has uncommitted changes. Commit or stash first.",
              file=sys.stderr)
        return []

    # Get findings
    all_findings = list_findings(repo_path)

    if not all_findings:
        print("No findings found.", file=sys.stderr)
        return []

    # Filter by status
    allowed_statuses = set(status_filter.split(","))
    candidates = [f for f in all_findings if f.get("status", "found") in allowed_statuses]

    # Enrich findings missing git data (older findings without churn/fan_out)
    needs_enrichment = [f for f in candidates if not f.get("churn_6m") and not f.get("fan_out")]
    if needs_enrichment:
        try:
            from git_enrichment import enrich_findings_batch
            enrich_findings_batch(needs_enrichment, repo_path, verbose=verbose)
        except Exception:
            pass

    if finding_ids:
        # Specific IDs requested
        id_set = set(finding_ids)
        targets = [f for f in candidates if f.get("id") in id_set]
        if len(targets) < len(id_set):
            found_ids = {f.get("id") for f in targets}
            missing = id_set - found_ids
            print(f"WARNING: IDs not found: {', '.join(missing)}", file=sys.stderr)
    else:
        # Sort by priority
        scan_history = load_scan_history(repo_path)
        for f in candidates:
            f["_priority"] = score_finding(f, scan_history, all_findings=all_findings)
        candidates.sort(key=lambda x: -x.get("_priority", 0))
        targets = candidates[:count]

    if not targets:
        print("No actionable findings after filtering.", file=sys.stderr)
        return []

    if verbose:
        print(f"\nDebt Loop: processing {len(targets)} finding(s)", file=sys.stderr)
        print(f"  Base branch: {base_branch}", file=sys.stderr)
        print(f"  Backend: {backend}", file=sys.stderr)
        print(f"  Dry run: {dry_run}", file=sys.stderr)

    results = []
    for f in targets:
        result = process_one_finding(
            f, repo_path, base_branch,
            model=model, backend=backend,
            dry_run=dry_run, verbose=verbose,
        )
        if result:
            results.append(result)

    # Summary
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Debt Loop Summary:", file=sys.stderr)
    for r in results:
        status_icon = {
            "pr_created": "✓",
            "pushed": "↑",
            "dry_run": "○",
            "skipped": "–",
            "no_fix": "✗",
            "apply_failed": "✗",
            "error": "!",
        }.get(r["status"], "?")
        pr = f" → {r['pr_url']}" if r.get("pr_url") else ""
        print(f"  {status_icon} {r['finding_id']}: {r['status']}{pr}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Automated debt resolution loop — pick top N findings, create fix PRs",
    )
    parser.add_argument("--repo", default=".", help="Path to git repository")
    parser.add_argument("--count", "-n", type=int, default=3,
                        help="Number of findings to process (default: 3)")
    parser.add_argument("--ids", default=None,
                        help="Comma-separated finding IDs to fix (overrides priority sort)")
    parser.add_argument("--model", default="claude-sonnet-4-20250514",
                        help="LLM model for fix generation")
    parser.add_argument("--backend", default="cli", choices=["cli", "api"],
                        help="LLM backend: cli ($0) or api (pay-per-use)")
    parser.add_argument("--base-branch", default=None,
                        help="Base branch for fix branches (default: current branch)")
    parser.add_argument("--status", default="found,verified",
                        help="Comma-separated statuses to include (default: found,verified)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate fixes but don't commit/push/PR")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show detailed progress")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")

    args = parser.parse_args()

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
        verbose=args.verbose,
    )

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))

    # Exit code: 0 if any PR created, 1 if all failed
    if any(r["status"] in ("pr_created", "pushed", "dry_run") for r in results):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
