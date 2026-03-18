"""
git_enrichment.py — Git-based churn and fan-out enrichment for findings.

Language-agnostic: uses git log for churn, git grep for references.
Designed to run at scan time (when we have access to the target repo)
so that values are stored in JSONL and available for dashboard/scoring
even when the dashboard is generated from a different directory.

Usage:
    from git_enrichment import enrich_finding, enrich_findings_batch

    # Single finding
    enrich_finding(finding_dict, repo_path)

    # Batch (more efficient — computes maps once)
    enrich_findings_batch(findings_list, repo_path)
"""

import re
import subprocess
from collections import Counter
from pathlib import Path


# ---------------------------------------------------------------------------
# Git churn: number of commits touching a file in last N months
# ---------------------------------------------------------------------------

def git_churn_map(repo_path: str, months: int = 6) -> dict[str, int]:
    """Compute file → commit_count for all files changed in last N months.

    Uses `git log --name-only` — works for any language.

    Returns:
        {relative_path: number_of_commits_touching_file}
    """
    try:
        result = subprocess.run(
            ["git", "log", f"--since={months} months ago",
             "--name-only", "--pretty=format:", "--diff-filter=ACMR"],
            capture_output=True, text=True,
            cwd=repo_path, timeout=30,
        )
        if result.returncode != 0:
            return {}

        files = [
            line.strip() for line in result.stdout.splitlines()
            if line.strip() and not line.startswith(" ")
        ]
        return dict(Counter(files))
    except Exception:
        return {}


def git_churn_file(repo_path: str, file_path: str, months: int = 6) -> int:
    """Get commit count for a single file."""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={months} months ago",
             "--oneline", "--", file_path],
            capture_output=True, text=True,
            cwd=repo_path, timeout=10,
        )
        if result.returncode != 0:
            return 0
        return len([l for l in result.stdout.splitlines() if l.strip()])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Git fan-out: number of files that reference a given file
# ---------------------------------------------------------------------------

def git_fan_out_map(repo_path: str) -> dict[str, int]:
    """Compute file → reference_count using git grep.

    Language-agnostic: searches for filename/module name references
    across all tracked files.

    Returns:
        {relative_path: number_of_files_referencing_it}
    """
    # Get all tracked files
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True,
            cwd=repo_path, timeout=10,
        )
        if result.returncode != 0:
            return {}
        all_files = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception:
        return {}

    if not all_files:
        return {}

    fan_out: dict[str, int] = {}

    # Build basename → full paths mapping
    basename_to_paths: dict[str, list[str]] = {}
    for fp in all_files:
        bn = Path(fp).stem  # filename without extension
        basename_to_paths.setdefault(bn, []).append(fp)

    # For each unique basename, count how many files reference it
    # Skip very common names that would cause noise
    skip_names = {
        "index", "main", "app", "test", "tests", "utils", "helpers",
        "config", "setup", "init", "__init__", "types", "constants",
        "mod", "lib", "package", "README", "LICENSE", "Makefile",
    }

    for basename, paths in basename_to_paths.items():
        if basename in skip_names:
            continue
        if len(basename) < 3:
            continue

        # Search for references to this module/file
        try:
            result = subprocess.run(
                ["git", "grep", "-l", "--fixed-strings", basename],
                capture_output=True, text=True,
                cwd=repo_path, timeout=5,
            )
            if result.returncode == 0:
                referencing_files = set(
                    l.strip() for l in result.stdout.splitlines() if l.strip()
                )
                # Subtract self-references
                ref_count = len(referencing_files - set(paths))
                for fp in paths:
                    fan_out[fp] = ref_count
        except (subprocess.TimeoutExpired, Exception):
            continue

    return fan_out


def git_fan_out_file(repo_path: str, file_path: str) -> int:
    """Get reference count for a single file."""
    stem = Path(file_path).stem
    if len(stem) < 3:
        return 0

    try:
        result = subprocess.run(
            ["git", "grep", "-l", "--fixed-strings", stem],
            capture_output=True, text=True,
            cwd=repo_path, timeout=5,
        )
        if result.returncode != 0:
            return 0
        referencing = set(l.strip() for l in result.stdout.splitlines() if l.strip())
        referencing.discard(file_path)
        return len(referencing)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Total lines (for entropy calculation)
# ---------------------------------------------------------------------------

def file_line_count(repo_path: str, file_path: str) -> int:
    """Get line count for a file."""
    full = Path(repo_path) / file_path
    if not full.exists():
        return 0
    try:
        return len(full.read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Enrichment API
# ---------------------------------------------------------------------------

def enrich_finding(finding: dict, repo_path: str) -> dict:
    """Enrich a single finding with churn, fan_out, total_lines from git.

    Modifies finding in-place and returns it.
    Only sets values if not already present (doesn't overwrite existing data).
    """
    loc = finding.get("location", {})
    file_a = loc.get("file_a", finding.get("file", ""))
    file_b = loc.get("file_b", "")

    if not file_a:
        return finding

    # Churn: max of both files
    if not finding.get("churn_6m"):
        churn_a = git_churn_file(repo_path, file_a)
        churn_b = git_churn_file(repo_path, file_b) if file_b else 0
        finding["churn_6m"] = max(churn_a, churn_b)

    # Fan-out: max of both files
    if not finding.get("fan_out"):
        fan_a = git_fan_out_file(repo_path, file_a)
        fan_b = git_fan_out_file(repo_path, file_b) if file_b else 0
        finding["fan_out"] = max(fan_a, fan_b)

    # Total lines (for entropy)
    if not finding.get("total_lines"):
        lines_a = file_line_count(repo_path, file_a)
        lines_b = file_line_count(repo_path, file_b) if file_b else 0
        finding["total_lines"] = max(lines_a, lines_b)

    return finding


def enrich_findings_batch(findings: list[dict], repo_path: str,
                          verbose: bool = False) -> list[dict]:
    """Enrich multiple findings efficiently using batch maps.

    Computes churn_map and fan_out_map once, then applies to all findings.
    Much faster than calling enrich_finding() per-finding for large batches.
    """
    import sys

    if verbose:
        print("  Computing git churn map...", file=sys.stderr)
    churn_map = git_churn_map(repo_path)

    if verbose:
        print(f"  Churn data: {len(churn_map)} files tracked", file=sys.stderr)
        print("  Computing fan-out map...", file=sys.stderr)
    fan_map = git_fan_out_map(repo_path)

    if verbose:
        print(f"  Fan-out data: {len(fan_map)} files with references", file=sys.stderr)

    for f in findings:
        loc = f.get("location", {})
        file_a = loc.get("file_a", f.get("file", ""))
        file_b = loc.get("file_b", "")

        if not file_a:
            continue

        # Churn: use batch map, max of both files
        if not f.get("churn_6m"):
            churn_a = churn_map.get(file_a, 0)
            churn_b = churn_map.get(file_b, 0) if file_b else 0
            f["churn_6m"] = max(churn_a, churn_b)

        # Fan-out: use batch map, max of both files
        if not f.get("fan_out"):
            fan_a = fan_map.get(file_a, 0)
            fan_b = fan_map.get(file_b, 0) if file_b else 0
            f["fan_out"] = max(fan_a, fan_b)

        # Total lines
        if not f.get("total_lines"):
            lines_a = file_line_count(repo_path, file_a)
            lines_b = file_line_count(repo_path, file_b) if file_b else 0
            f["total_lines"] = max(lines_a, lines_b)

    if verbose:
        enriched = sum(1 for f in findings if f.get("churn_6m", 0) > 0 or f.get("fan_out", 0) > 0)
        print(f"  Enriched {enriched}/{len(findings)} findings with git data", file=sys.stderr)

    return findings
