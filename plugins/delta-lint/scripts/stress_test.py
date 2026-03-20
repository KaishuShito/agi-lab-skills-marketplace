"""
Stress-test engine for delta-lint.

Generates virtual modifications and runs scan on each to build a
per-file "landmine map" showing which areas break most easily.

Pipeline:
  Step 0:   Structural analysis (claude -p, $0)
  Step 0.5: Existing bug scan — scan hotspot clusters for current contradictions
  Step 1:   Virtual modification generation (claude -p, $0)
  Step 2:   Scan each modification (existing detect engine, claude -p, $0)

All LLM calls use claude -p (subscription CLI) for $0 cost.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time as _time
from datetime import datetime
from pathlib import Path

from retrieval import (
    ModuleContext,
    FileContext,
    build_context,
    filter_source_files,
    _read_file_safe,
)
from detector import detect


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROMPT_DIR = Path(__file__).parent / "prompts"
HEAD_LINES = 50  # Lines to read from each file for structural analysis
MAX_FILES_FOR_STRUCTURE = 80  # Cap files sent to structure analysis


def _is_git_repo(path: str) -> bool:
    """Check if path is inside a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, cwd=path, timeout=5,
    )
    return result.returncode == 0


# Directories to skip when walking filesystem (no .gitignore available)
_WALK_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", ".output",
    "vendor", "target", "out", ".gradle", ".idea", ".vscode",
    "coverage", ".nyc_output", ".turbo", ".cache",
}


def _list_source_files(repo_path: str, verbose: bool = False) -> list[str]:
    """List source files — git ls-files if available, else filesystem walk.

    Returns relative paths from repo_path.
    """
    if _is_git_repo(repo_path):
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        files = filter_source_files(result.stdout.strip().split("\n"))
        if files:
            return files

    # Fallback: filesystem walk (no .gitignore support)
    if verbose:
        print("  [warn] git not available — using filesystem walk (精度が下がります)", file=sys.stderr)
    repo = Path(repo_path)
    found = []
    for root, dirs, filenames in os.walk(repo_path):
        # Prune skipped directories in-place
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            full = Path(root) / fname
            try:
                rel = str(full.relative_to(repo))
                found.append(rel)
            except ValueError:
                pass
    return filter_source_files(found)


def _sample_across_dirs(files: list[str], max_count: int) -> list[str]:
    """Sample files evenly across top-level directories.

    Avoids alphabetical bias where e.g. 'apps/design-system' consumes
    all slots before 'apps/studio' is reached.
    """
    from collections import defaultdict

    # Group by top 2 directory levels (e.g. "apps/studio")
    groups: dict[str, list[str]] = defaultdict(list)
    for f in files:
        parts = f.split("/")
        key = "/".join(parts[:min(2, len(parts))])
        groups[key].append(f)

    # Round-robin across groups
    sampled: list[str] = []
    group_iters = {k: iter(v) for k, v in sorted(groups.items())}

    while len(sampled) < max_count and group_iters:
        exhausted = []
        for key, it in group_iters.items():
            if len(sampled) >= max_count:
                break
            val = next(it, None)
            if val is None:
                exhausted.append(key)
            else:
                sampled.append(val)
        for key in exhausted:
            del group_iters[key]

    return sampled


def _sample_by_churn(
    files: list[str],
    churn_data: list[dict],
    max_count: int,
) -> list[str]:
    """Sample files weighted by git change frequency.

    Prioritizes frequently modified files (top 50% of slots),
    then fills remaining slots evenly across directories.
    This ensures stress test analyzes code developers actually touch.
    """
    # Build churn lookup: path → change count
    churn_map = {item["path"]: item["changes"] for item in churn_data}

    # Split: files with churn data vs without
    churned = [(f, churn_map[f]) for f in files if f in churn_map]
    churned.sort(key=lambda x: x[1], reverse=True)

    # Allocate: top 50% for high-churn, rest for directory diversity
    churn_slots = max_count // 2
    diversity_slots = max_count - churn_slots

    sampled: list[str] = []
    seen: set[str] = set()

    # Phase 1: High-churn files
    for f, _count in churned:
        if len(sampled) >= churn_slots:
            break
        if f in seen:
            continue
        sampled.append(f)
        seen.add(f)

    # Phase 2: Directory diversity (excluding already-selected)
    remaining = [f for f in files if f not in seen]
    diverse = _sample_across_dirs(remaining, diversity_slots)
    for f in diverse:
        if f not in seen:
            sampled.append(f)
            seen.add(f)

    return sampled[:max_count]


# ---------------------------------------------------------------------------
# Progressive scan coverage tracking
# ---------------------------------------------------------------------------

def _coverage_path(repo_path: str) -> Path:
    return Path(repo_path) / ".delta-lint" / "scan_coverage.json"


def load_coverage(repo_path: str) -> dict:
    """Load scan coverage data.

    Returns:
        {
            "scanned_files": {"path": {"last_scanned": "2026-03-18T...", "scan_count": 3, "findings": 1}},
            "scanned_dirs": {"dir/": {"last_scanned": "...", "file_count": 5}},
            "total_scans": 5,
            "last_scan": "2026-03-18T...",
        }
    """
    path = _coverage_path(repo_path)
    if not path.exists():
        return {"scanned_files": {}, "scanned_dirs": {}, "total_scans": 0, "last_scan": ""}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"scanned_files": {}, "scanned_dirs": {}, "total_scans": 0, "last_scan": ""}


def save_coverage(repo_path: str, coverage: dict) -> Path:
    """Save scan coverage data."""
    path = _coverage_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(coverage, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def update_coverage_from_results(
    repo_path: str,
    results: list[dict],
    existing_results: list[dict] | None = None,
) -> dict:
    """Update scan coverage from stress-test results.

    Tracks which files have been analyzed, when, and how many findings.
    """
    coverage = load_coverage(repo_path)
    scanned = coverage["scanned_files"]
    scanned_dirs = coverage["scanned_dirs"]
    now = datetime.now().isoformat()

    # From stress-test results (virtual modifications)
    for r in results:
        mod = r.get("modification", {})
        target = mod.get("file", "")
        affected = mod.get("affected_files", [])
        n_findings = len(r.get("findings", []))

        all_files = [target] + affected
        for f in all_files:
            if not f or f == "[virtual-modification]":
                continue
            if f not in scanned:
                scanned[f] = {"last_scanned": now, "scan_count": 0, "findings": 0}
            scanned[f]["last_scanned"] = now
            scanned[f]["scan_count"] = scanned[f].get("scan_count", 0) + 1
            if n_findings > 0:
                scanned[f]["findings"] = scanned[f].get("findings", 0) + n_findings

    # From existing scan results (hotspot clusters)
    if existing_results:
        for r in existing_results:
            cluster = r.get("cluster", {})
            for f in cluster.get("files", []):
                if f not in scanned:
                    scanned[f] = {"last_scanned": now, "scan_count": 0, "findings": 0}
                scanned[f]["last_scanned"] = now
                scanned[f]["scan_count"] = scanned[f].get("scan_count", 0) + 1
                n_findings = len(r.get("findings", []))
                if n_findings > 0:
                    scanned[f]["findings"] = scanned[f].get("findings", 0) + n_findings

    # Update directory coverage
    for f in scanned:
        parts = f.split("/")
        if len(parts) > 1:
            dir_key = "/".join(parts[:min(3, len(parts) - 1)]) + "/"
            if dir_key not in scanned_dirs:
                scanned_dirs[dir_key] = {"last_scanned": now, "file_count": 0}
            scanned_dirs[dir_key]["last_scanned"] = now

    # Recount files per dir
    from collections import Counter
    dir_counts = Counter()
    for f in scanned:
        parts = f.split("/")
        if len(parts) > 1:
            dir_key = "/".join(parts[:min(3, len(parts) - 1)]) + "/"
            dir_counts[dir_key] += 1
    for d, count in dir_counts.items():
        if d in scanned_dirs:
            scanned_dirs[d]["file_count"] = count

    coverage["scanned_files"] = scanned
    coverage["scanned_dirs"] = scanned_dirs
    coverage["total_scans"] = coverage.get("total_scans", 0) + 1
    coverage["last_scan"] = now

    save_coverage(repo_path, coverage)
    return coverage


def get_files_changed_since_last_scan(repo_path: str) -> list[str]:
    """Get files that changed (git) since last scan coverage update.

    These files should be re-scanned even if already covered.
    """
    coverage = load_coverage(repo_path)
    last_scan = coverage.get("last_scan", "")
    if not last_scan:
        return []

    try:
        # Get files changed since last scan timestamp
        result = subprocess.run(
            ["git", "log", f"--since={last_scan}", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        if result.returncode != 0:
            return []
        changed = set()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                changed.add(line)
        return list(changed)
    except Exception:
        return []


def prioritize_files_for_scan(
    all_files: list[str],
    repo_path: str,
    churn_data: list[dict] | None = None,
    max_count: int = 80,
    verbose: bool = False,
) -> list[str]:
    """Progressive file selection: uncovered first, then stale, then fresh.

    Priority order:
    1. Files changed since last scan (re-scan needed)
    2. Never-scanned files with high git churn (blind spots in hot areas)
    3. Never-scanned files in uncovered directories
    4. Previously scanned files (oldest first, for refresh)

    Each category fills a portion of the budget, ensuring expansion.
    """
    coverage = load_coverage(repo_path)
    scanned = coverage.get("scanned_files", {})
    churn_map = {item["path"]: item["changes"] for item in (churn_data or [])}

    # Classify files
    changed_since = set(get_files_changed_since_last_scan(repo_path))
    never_scanned = []
    stale = []  # scanned but changed since
    fresh = []  # scanned and not changed

    for f in all_files:
        if f in changed_since and f in scanned:
            stale.append(f)
        elif f not in scanned:
            never_scanned.append(f)
        else:
            fresh.append(f)

    # Sort never-scanned by churn (high churn = higher priority)
    never_scanned.sort(key=lambda f: churn_map.get(f, 0), reverse=True)

    # Sort fresh by last_scanned (oldest first = needs refresh)
    fresh.sort(key=lambda f: scanned.get(f, {}).get("last_scanned", ""))

    # Budget allocation
    #   - Changed since last scan: up to 30%
    #   - Never scanned (high churn): up to 40%
    #   - Never scanned (directory diversity): up to 20%
    #   - Stale refresh: remaining 10%
    changed_budget = max(max_count * 30 // 100, 1)
    new_churn_budget = max(max_count * 40 // 100, 1)
    new_diverse_budget = max(max_count * 20 // 100, 1)
    refresh_budget = max(max_count * 10 // 100, 1)

    selected: list[str] = []
    seen: set[str] = set()

    def _add(files: list[str], budget: int):
        added = 0
        for f in files:
            if added >= budget:
                break
            if f not in seen:
                selected.append(f)
                seen.add(f)
                added += 1

    # 1. Changed since last scan
    _add(stale, changed_budget)

    # 2. Never scanned, high churn
    _add(never_scanned, new_churn_budget)

    # 3. Never scanned, directory diversity
    remaining_new = [f for f in never_scanned if f not in seen]
    diverse_new = _sample_across_dirs(remaining_new, new_diverse_budget)
    _add(diverse_new, new_diverse_budget)

    # 4. Refresh oldest scanned
    _add(fresh, refresh_budget)

    if verbose:
        n_changed = min(len(stale), changed_budget)
        n_new = len([f for f in selected if f in set(never_scanned)])
        n_refresh = len(selected) - n_changed - n_new
        total_covered = len(scanned)
        total_files = len(all_files)
        pct = round(total_covered / max(total_files, 1) * 100)
        print(f"  [progressive] Coverage: {total_covered}/{total_files} files ({pct}%)", file=sys.stderr)
        print(f"  [progressive] This scan: {n_changed} re-scan + {n_new} new + {n_refresh} refresh = {len(selected)}", file=sys.stderr)

    return selected[:max_count]


# ---------------------------------------------------------------------------
# Git history context for LLM
# ---------------------------------------------------------------------------

def _build_git_history_context(repo_path: str, months: int = 6, verbose: bool = False) -> str:
    """Build a human-readable git history summary for the LLM.

    Groups commits by directory and extracts:
    - Recent commit messages (showing what kind of work is happening)
    - Per-file change counts (churn)
    - Author distribution (knowledge silo detection)
    - Co-change patterns (files that move together)

    Returns a markdown-formatted string to inject into the structure analysis prompt.
    """
    if not _is_git_repo(repo_path):
        return ""

    sections = []

    # 1. Per-directory commit summaries (most recent 8 per directory)
    try:
        result = subprocess.run(
            ["git", "log", f"--since={months} months ago",
             "--pretty=format:%s|||%an", "--name-only"],
            capture_output=True, text=True, cwd=repo_path, timeout=30,
        )
        if result.returncode != 0:
            return ""

        from collections import defaultdict, Counter

        # Parse: group commit messages + authors by directory
        dir_commits: dict[str, list[str]] = defaultdict(list)
        dir_authors: dict[str, Counter] = defaultdict(Counter)
        current_msg = ""
        current_author = ""

        for line in result.stdout.strip().split("\n"):
            if "|||" in line:
                parts = line.split("|||", 1)
                current_msg = parts[0].strip()
                current_author = parts[1].strip() if len(parts) > 1 else ""
            elif line.strip():
                filepath = line.strip()
                # Group by top 2 directory levels
                parts = filepath.split("/")
                dir_key = "/".join(parts[:min(3, len(parts) - 1)]) + "/" if len(parts) > 1 else "(root)"
                if current_msg and len(dir_commits[dir_key]) < 15:
                    dir_commits[dir_key].append(current_msg)
                if current_author:
                    dir_authors[dir_key][current_author] += 1

        # Sort directories by commit count (most active first)
        sorted_dirs = sorted(dir_commits.keys(), key=lambda d: len(dir_commits[d]), reverse=True)

        dir_sections = []
        for d in sorted_dirs[:15]:  # Top 15 directories
            msgs = dir_commits[d]
            authors = dir_authors[d]

            lines = [f"### {d} ({len(msgs)} commits)"]

            # Recent commit messages
            lines.append("Recent changes:")
            for msg in msgs[:8]:
                lines.append(f"  - {msg[:100]}")

            # Author distribution
            if authors:
                total = sum(authors.values())
                top_author = authors.most_common(1)[0]
                if top_author[1] / total > 0.8 and total >= 3:
                    lines.append(f"⚠ Single-owner risk: {top_author[0]} ({top_author[1]}/{total} commits)")
                else:
                    author_str = ", ".join(f"{a}({c})" for a, c in authors.most_common(3))
                    lines.append(f"Authors: {author_str}")

            dir_sections.append("\n".join(lines))

        if dir_sections:
            sections.append("## Git History (last {} months)\n\n{}".format(
                months, "\n\n".join(dir_sections)
            ))

    except Exception:
        pass

    # 2. Top co-change pairs (files that change together)
    try:
        from sibling import get_git_churn
        churn = get_git_churn(repo_path, months=months)
        if churn:
            churn_lines = ["## File Change Frequency (top 20)"]
            for item in churn[:20]:
                churn_lines.append(f"- {item['path']}: {item['changes']} changes")
            sections.append("\n".join(churn_lines))
    except Exception:
        pass

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Step 0: Structural analysis
# ---------------------------------------------------------------------------

def analyze_structure(
    repo_path: str,
    verbose: bool = False,
    churn_data: list[dict] | None = None,
) -> dict:
    """Analyze codebase structure via claude -p.

    Reads file headers and asks LLM to identify roles, dependencies,
    and implicit constraints.

    Args:
        churn_data: Optional git churn data from sibling.get_git_churn().
            If provided, frequently modified files are prioritized in sampling.
    """
    if verbose:
        print("[step 0] Analyzing codebase structure...", file=sys.stderr)

    repo = Path(repo_path)

    # Get source files — prefer git ls-files, fallback to filesystem walk
    all_files = _list_source_files(repo_path, verbose=verbose)

    if verbose:
        print(f"  Found {len(all_files)} source files", file=sys.stderr)

    # Sample: progressive (coverage-aware) > churn-weighted > directory diversity
    coverage = load_coverage(repo_path)
    has_prior_scans = coverage.get("total_scans", 0) > 0

    if has_prior_scans:
        # Progressive: prioritize uncovered + changed files
        files_to_analyze = prioritize_files_for_scan(
            all_files, repo_path, churn_data=churn_data,
            max_count=MAX_FILES_FOR_STRUCTURE, verbose=verbose,
        )
        if verbose:
            print(f"  Using progressive sampling ({len(files_to_analyze)} files)", file=sys.stderr)
    elif churn_data:
        files_to_analyze = _sample_by_churn(all_files, churn_data, MAX_FILES_FOR_STRUCTURE)
        if verbose:
            print(f"  Using churn-weighted sampling ({len(files_to_analyze)} files)", file=sys.stderr)
    else:
        files_to_analyze = _sample_across_dirs(all_files, MAX_FILES_FOR_STRUCTURE)

    # Read first N lines of each file
    file_previews = []
    for fpath in files_to_analyze:
        full = repo / fpath
        if not full.exists():
            continue
        content = _read_file_safe(full)
        if content is None:
            continue
        lines = content.split("\n")[:HEAD_LINES]
        preview = "\n".join(lines)
        file_previews.append(f"=== {fpath} ===\n{preview}")

    # Load prompt template
    prompt_template = (PROMPT_DIR / "structure_analysis.md").read_text(encoding="utf-8")

    # Build git history context (injected between prompt and file previews)
    git_context = _build_git_history_context(repo_path, months=6, verbose=verbose)
    if git_context:
        prompt = prompt_template + "\n\n" + git_context + "\n\n## Source Files\n\n" + "\n\n".join(file_previews)
        if verbose:
            print(f"  Injecting git history context ({len(git_context)} chars)", file=sys.stderr)
    else:
        prompt = prompt_template + "\n\n" + "\n\n".join(file_previews)

    # Truncate if too large
    if len(prompt) > 80_000:
        prompt = prompt[:80_000] + "\n... (truncated)"

    if verbose:
        print(f"  Sending {len(file_previews)} file previews to claude -p ({len(prompt)} chars)", file=sys.stderr)

    raw = _call_claude(prompt)
    structure = _parse_json_response(raw)

    if verbose:
        modules = structure.get("modules", [])
        hotspots = structure.get("hotspots", [])
        print(f"  Identified {len(modules)} modules, {len(hotspots)} hotspots", file=sys.stderr)

    return structure


def init_lightweight(
    repo_path: str,
    verbose: bool = False,
) -> dict:
    """Lightweight init — Step 0 only (structure analysis).

    Fast (~30 seconds, 1 LLM call). Generates:
    - .delta-lint/stress-test/structure.json
    - .delta-lint/constraints.yml (scaffold, if not exists)
    - .delta-lint/sibling_map.yml (from git co-change history)

    Safe to re-run: structure.json is overwritten, constraints.yml is never touched.
    Sibling map merges new entries without overwriting existing ones.
    """
    repo_path = str(Path(repo_path).resolve())
    out = Path(repo_path) / ".delta-lint" / "stress-test"
    out.mkdir(parents=True, exist_ok=True)

    # Pre-step: Get git churn data for churn-weighted sampling
    churn_data = []
    try:
        from sibling import get_git_churn
        churn_data = get_git_churn(repo_path, months=6)
        if verbose and churn_data:
            print(f"  Git churn: {len(churn_data)} files with change history", file=sys.stderr)
            for item in churn_data[:5]:
                print(f"    {item['path']}: {item['changes']} changes", file=sys.stderr)
    except Exception:
        pass

    # Step 0: Structure analysis (churn-weighted sampling)
    structure = analyze_structure(repo_path, verbose=verbose, churn_data=churn_data)
    structure_path = out / "structure.json"
    structure_path.write_text(
        json.dumps(structure, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        print(f"  Saved: {structure_path}", file=sys.stderr)

    # Generate constraints.yml scaffold (never overwrite existing)
    constraints_path = Path(repo_path) / ".delta-lint" / "constraints.yml"
    if not constraints_path.exists():
        modules = structure.get("modules", [])
        lines = [
            "# delta-lint constraints — known invariants for this repository",
            "# Add rules that the code must follow but aren't obvious from reading it.",
            "# These are used by `delta scan` to improve detection accuracy.",
            "#",
            "# How to add: edit this file directly, or tell Claude Code:",
            '#   "この関数は消費税を切り捨てで計算してる。前提として登録して"',
            "#",
            "# This file is NEVER overwritten by `delta init`.",
            "",
            "constraints:",
        ]

        policy_lines = [
            "",
            "# ── チームポリシー ──",
            "# チームごとに「何を負債とみなすか」をカスタマイズできます。",
            "#",
            "# policy:",
            "#   # メンバーの権限レベル（承認の信憑性を可視化）",
            "#   roles:",
            "#     tanaka: lead       # suppress/accepted の承認権限あり",
            "#     suzuki: senior",
            "#     yamada: junior     # 未承認の suppress はダッシュボードで警告表示",
            "#",
            "#   # アーキテクチャ文脈 — LLM に渡して誤検出を減らす",
            "#   architecture:",
            '#     - "モノリスからマイクロサービスへの移行中。サービス間の重複は過渡期として許容"',
            '#     - "認証はOAuth2+セッション併用。意図的な二重管理であり矛盾ではない"',
            "#",
            "#   # 許容済み — 意図的なトレードオフとして受け入れた検出結果",
            "#   accepted:",
            '#     - id: "myrepo-a1b2c3d4"',
            '#       reason: "仕様通りの挙動"',
            '#       approved_by: tanaka    # lead が承認',
            '#     - pattern: "④"',
            '#       file: "src/legacy/*"',
            '#       reason: "レガシー層は段階的リプレース予定。Q3で解消"',
            '#       approved_by: tanaka',
            "#",
            "#   # 重大度の上書き — チームの文脈に合わせてリスクを調整",
            "#   severity_overrides:",
            '#     - pattern: "①"',
            '#       file: "src/api/*"',
            '#       severity: "high"  # API層の障害はうちでは致命的',
            "#",
            "#   # 負債バジェット — CI ゲート。未解決スコアがこれを超えたら scan 失敗",
            "#   # 0 = ゼロトレランス（全件 accepted か resolved でないと通らない）",
            "#   debt_budget: 0",
        ]
        # Pre-populate with auto-extracted constraints as examples
        added = 0
        for mod in modules:
            mod_path = mod.get("path", "")
            items = mod.get("implicit_constraints", [])
            if items and added < 5:
                lines.append(f"  - file: \"{mod_path}\"")
                lines.append(f"    rules:")
                for item in items[:3]:
                    lines.append(f"      - \"{item}\"")
                added += 1
        if added == 0:
            lines.append("  # - file: \"src/billing/invoice.ts\"")
            lines.append("  #   rules:")
            lines.append("  #     - \"Tax calculation uses floor rounding (not round)\"")

        constraints_path.write_text("\n".join(lines + policy_lines) + "\n", encoding="utf-8")
        if verbose:
            print(f"  Created: {constraints_path}", file=sys.stderr)

    return structure


# ---------------------------------------------------------------------------
# Step 0.5: Existing bug scan — scan hotspot clusters directly
# ---------------------------------------------------------------------------

_EXISTING_LANG_INSTRUCTIONS = {
    "en": "",
    "ja": (
        "## Language\n\n"
        "Write the `contradiction`, `user_impact`, `reproduction`, and `internal_evidence` fields in **Japanese**. "
        "Keep `pattern`, `severity`, `bug_class`, and `location` fields in English/emoji. "
        "Example: `\"user_impact\": \"デフォルト設定でLoRAファインチューニングを実行するとAttributeErrorでクラッシュする\"`"
    ),
}


def _load_existing_prompt(lang: str = "en") -> str:
    """Load the existing-bug-specific detection prompt."""
    prompt = (PROMPT_DIR / "detect_existing.md").read_text(encoding="utf-8")
    lang_instruction = _EXISTING_LANG_INSTRUCTIONS.get(lang, "")
    return prompt.replace("{lang_instruction}", lang_instruction)


def _scan_cluster(
    cluster: dict,
    index: int,
    total: int,
    repo_path: str,
    backend: str,
    verbose: bool,
    lang: str = "en",
) -> dict:
    """Scan a file cluster for existing contradictions. Thread-safe.

    Uses detect_existing.md prompt which classifies findings as:
    🔴 実バグ / 🟡 サイレント障害 / ⚪ 潜在リスク
    and requires concrete user_impact and reproduction fields.
    """
    center = cluster["center"]
    files = cluster["files"]

    if verbose:
        print(f"[step 0.5] [{index}/{total}] Scanning cluster: {center}", file=sys.stderr)

    last_error = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            context = build_context(repo_path, files)

            if not context.target_files:
                if verbose:
                    print(f"  [{index}/{total}] Skipped (no readable files)", file=sys.stderr)
                return {"cluster": cluster, "findings": []}

            # Use existing-bug-specific prompt (not the stress-test one)
            system_prompt = _load_existing_prompt(lang=lang)
            from detector import build_user_prompt, _parse_response, _detect_cli, _cli_available
            user_prompt = build_user_prompt(context, repo_name=Path(repo_path).name)

            if backend == "cli" and _cli_available():
                raw = _detect_cli(system_prompt, user_prompt)
            else:
                # Fallback to standard detect with default prompt
                findings = detect(
                    context,
                    repo_name=Path(repo_path).name,
                    backend=backend,
                )
                findings = [f for f in findings if not f.get("parse_error")]
                if verbose:
                    print(f"  [{index}/{total}] Found {len(findings)} finding(s) (fallback prompt)", file=sys.stderr)
                return {"cluster": cluster, "findings": findings}

            findings = _parse_response(raw)
            findings = [f for f in findings if not f.get("parse_error")]

            if verbose:
                print(f"  [{index}/{total}] Found {len(findings)} finding(s)", file=sys.stderr)

            return {"cluster": cluster, "findings": findings}

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                if verbose:
                    print(f"  [{index}/{total}] Retry ({e})", file=sys.stderr)
            else:
                if verbose:
                    print(f"  [{index}/{total}] Failed: {e}", file=sys.stderr)

    return {"cluster": cluster, "findings": [], "error": str(last_error)}


def _build_clusters(
    structure: dict,
    repo_path: str,
    verbose: bool = False,
    depth: int = 2,
) -> list[dict]:
    """Build file clusters from hotspots with deep dependency traversal.

    Args:
        depth: How many hops to follow dependencies (1=direct only, 2=2-hop).
    """
    hotspots = structure.get("hotspots", [])
    modules = structure.get("modules", [])

    if not hotspots:
        return []

    # Build dependency lookup (forward + reverse)
    dep_map: dict[str, list[str]] = {}
    rev_dep_map: dict[str, list[str]] = {}
    for mod in modules:
        path = mod.get("path", "")
        deps = mod.get("dependencies", [])
        if path:
            dep_map[path] = deps
            for dep in deps:
                rev_dep_map.setdefault(dep, []).append(path)

    # Load sibling_map for co-change pairs
    sibling_pairs: dict[str, list[str]] = {}
    try:
        from sibling import load_sibling_map
        for entry in load_sibling_map(repo_path):
            sibling_pairs.setdefault(entry.file_a, []).append(entry.file_b)
            sibling_pairs.setdefault(entry.file_b, []).append(entry.file_a)
        if verbose and sibling_pairs:
            print(f"  Loaded sibling_map: {len(sibling_pairs)} files with siblings", file=sys.stderr)
    except Exception:
        pass

    def _expand(seed: str, max_depth: int) -> list[str]:
        """BFS expansion: forward deps + reverse deps + siblings up to max_depth hops."""
        visited = {seed}
        frontier = [seed]
        for _ in range(max_depth):
            next_frontier = []
            for f in frontier:
                for neighbor in dep_map.get(f, []) + rev_dep_map.get(f, []) + sibling_pairs.get(f, []):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.append(neighbor)
            frontier = next_frontier
            if not frontier:
                break
        return list(visited)

    clusters: list[dict] = []
    seen_centers: set[str] = set()

    for hs in hotspots:
        center = hs.get("path", hs.get("file", ""))
        if not center or center in seen_centers:
            continue
        seen_centers.add(center)

        files = _expand(center, depth)

        clusters.append({
            "center": center,
            "reason": hs.get("reason", ""),
            "files": files,
        })

    if verbose:
        avg_size = sum(len(c["files"]) for c in clusters) / max(len(clusters), 1)
        print(f"  {len(clusters)} clusters (avg {avg_size:.1f} files/cluster, depth={depth})", file=sys.stderr)

    return clusters


def _escalate_clusters(
    structure: dict,
    repo_path: str,
    existing_clusters: list[dict],
    verbose: bool = False,
) -> list[dict]:
    """Generate additional clusters for escalation when initial scan finds 0 findings.

    Strategy:
    1. Merge small clusters into larger cross-cutting ones
    2. Add clusters from sibling_map pairs not already covered
    3. Add clusters from high-churn files not in hotspots
    """
    modules = structure.get("modules", [])
    covered_files = {f for c in existing_clusters for f in c["files"]}
    new_clusters: list[dict] = []

    # Strategy 1: Merge adjacent clusters to create cross-cutting views
    if len(existing_clusters) >= 2:
        merged_files: list[str] = []
        for c in existing_clusters:
            for f in c["files"]:
                if f not in merged_files:
                    merged_files.append(f)
        # Cap at 15 files to keep context manageable
        if len(merged_files) > 1:
            new_clusters.append({
                "center": "(cross-cutting)",
                "reason": "escalation: merged hotspot clusters for wider view",
                "files": merged_files[:15],
            })

    # Strategy 2: Sibling pairs not yet covered
    try:
        from sibling import load_sibling_map
        for entry in load_sibling_map(repo_path):
            if entry.file_a not in covered_files or entry.file_b not in covered_files:
                new_clusters.append({
                    "center": entry.file_a,
                    "reason": f"escalation: sibling pair ({entry.contract})",
                    "files": [entry.file_a, entry.file_b],
                })
                covered_files.update([entry.file_a, entry.file_b])
                if len(new_clusters) >= 5:
                    break
    except Exception:
        pass

    # Strategy 3: High-churn files not in hotspots
    try:
        from sibling import get_git_churn
        churn = get_git_churn(repo_path, months=6)
        for item in churn[:10]:
            path = item.get("path", "")
            if path and path not in covered_files:
                # Build a small cluster around this churn-heavy file
                dep_map: dict[str, list[str]] = {}
                for mod in modules:
                    p = mod.get("path", "")
                    if p:
                        dep_map[p] = mod.get("dependencies", [])
                files = [path] + [d for d in dep_map.get(path, []) if d not in covered_files][:4]
                new_clusters.append({
                    "center": path,
                    "reason": f"escalation: high churn ({item.get('changes', '?')} changes in 6m)",
                    "files": files,
                })
                covered_files.update(files)
                if len(new_clusters) >= 8:
                    break
    except Exception:
        pass

    if verbose:
        print(f"  [escalation] Generated {len(new_clusters)} additional clusters", file=sys.stderr)

    return new_clusters


def _run_clusters(
    clusters: list[dict],
    repo_path: str,
    backend: str,
    verbose: bool,
    parallel: int,
    lang: str,
    stream: bool,
    label: str = "step 0.5",
):
    """Run scan on clusters. Yields or returns results depending on stream flag."""
    total = len(clusters)
    CLUSTER_TIMEOUT = 600

    if not stream:
        if parallel <= 1:
            return [
                _scan_cluster(c, i, total, repo_path, backend, verbose, lang=lang)
                for i, c in enumerate(clusters, 1)
            ]

        from concurrent.futures import ThreadPoolExecutor, as_completed

        if verbose:
            print(f"[{label}] Running {total} cluster scans with {parallel} workers", file=sys.stderr)

        results = [None] * total
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {
                pool.submit(_scan_cluster, c, i, total, repo_path, backend, verbose, lang): i - 1
                for i, c in enumerate(clusters, 1)
            }
            for future in as_completed(futures, timeout=CLUSTER_TIMEOUT + 60):
                idx = futures[future]
                try:
                    results[idx] = future.result(timeout=CLUSTER_TIMEOUT)
                except (TimeoutError, Exception) as exc:
                    results[idx] = {"findings": [], "error": str(exc)}
                    if verbose:
                        print(f"[error] cluster {idx+1}/{total}: {exc}", file=sys.stderr)

        return [r if r is not None else {"findings": [], "error": "incomplete"} for r in results]

    # Streaming mode
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if verbose:
        print(f"[{label}] Streaming {total} cluster scans with {parallel} workers", file=sys.stderr)

    completed = 0
    if parallel <= 1:
        for i, c in enumerate(clusters, 1):
            result = _scan_cluster(c, i, total, repo_path, backend, verbose, lang=lang)
            completed += 1
            yield result, completed, total
    else:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {
                pool.submit(_scan_cluster, c, i, total, repo_path, backend, verbose, lang): i - 1
                for i, c in enumerate(clusters, 1)
            }
            for future in as_completed(futures, timeout=CLUSTER_TIMEOUT + 60):
                try:
                    result = future.result(timeout=CLUSTER_TIMEOUT)
                except (TimeoutError, Exception) as exc:
                    result = {"findings": [], "error": str(exc)}
                    if verbose:
                        print(f"[error] cluster scan: {exc}", file=sys.stderr)
                completed += 1
                yield result, completed, total


def scan_existing(
    structure: dict,
    repo_path: str,
    backend: str = "cli",
    verbose: bool = False,
    parallel: int = 1,
    lang: str = "en",
    stream: bool = False,
):
    """Scan hotspot file clusters for existing contradictions.

    Uses deep cluster building (2-hop deps + sibling_map + reverse deps)
    then runs detect() on each cluster WITHOUT virtual modifications.
    This finds bugs that exist RIGHT NOW in the codebase.

    If initial scan finds 0 findings, automatically escalates with
    expanded clusters (merged cross-cutting, sibling pairs, high-churn files).

    If stream=True, yields (result, completed_count, total_count) tuples
    as each cluster completes (completion order, not index order).
    If stream=False (default), returns list[dict] for backward compatibility.
    """
    if verbose:
        print("[step 0.5] Scanning for existing contradictions...", file=sys.stderr)

    # Phase 1: Build deep clusters (2-hop deps + siblings + reverse deps)
    clusters = _build_clusters(structure, repo_path, verbose=verbose, depth=2)

    if not clusters:
        if verbose:
            print("  No hotspots found, skipping existing scan", file=sys.stderr)
        if stream:
            return
        return []

    if not stream:
        # Batch mode with auto-escalation
        results = _run_clusters(clusters, repo_path, backend, verbose, parallel, lang,
                                stream=False, label="step 0.5")

        # Auto-escalation: if 0 findings, expand and retry
        total_findings = sum(len(r.get("findings", [])) for r in results)
        if total_findings == 0:
            if verbose:
                print("[step 0.5] 0 findings — escalating with expanded clusters...", file=sys.stderr)
            esc_clusters = _escalate_clusters(structure, repo_path, clusters, verbose=verbose)
            if esc_clusters:
                esc_results = _run_clusters(esc_clusters, repo_path, backend, verbose, parallel, lang,
                                            stream=False, label="escalation")
                results.extend(esc_results)

        return results

    # Streaming mode with auto-escalation
    all_results: list[dict] = []
    total_findings = 0

    for result, completed, total in _run_clusters(
        clusters, repo_path, backend, verbose, parallel, lang,
        stream=True, label="step 0.5",
    ):
        all_results.append(result)
        total_findings += len(result.get("findings", []))
        yield result, completed, total

    # Auto-escalation: if 0 findings, expand and retry
    if total_findings == 0:
        if verbose:
            print("[step 0.5] 0 findings — escalating with expanded clusters...", file=sys.stderr)
        esc_clusters = _escalate_clusters(structure, repo_path, clusters, verbose=verbose)
        if esc_clusters:
            for result, completed, total in _run_clusters(
                esc_clusters, repo_path, backend, verbose, parallel, lang,
                stream=True, label="escalation",
            ):
                yield result, completed, total


# ---------------------------------------------------------------------------
# Step 1: Generate virtual modifications
# ---------------------------------------------------------------------------

def generate_modifications(
    structure: dict,
    repo_path: str,
    n: int = 25,
    verbose: bool = False,
) -> list[dict]:
    """Generate virtual modifications via claude -p.

    Uses structural analysis + git history to create realistic
    virtual code changes for stress testing.
    """
    if verbose:
        print(f"[step 1] Generating {n} virtual modifications...", file=sys.stderr)

    # Get recent git log (optional — empty if not a git repo)
    git_log = ""
    if _is_git_repo(repo_path):
        result = subprocess.run(
            ["git", "log", "--oneline", "-50"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        git_log = result.stdout.strip()

    # Load prompt template
    prompt_template = (PROMPT_DIR / "generate_modifications.md").read_text(encoding="utf-8")
    prompt = prompt_template.replace("{n}", str(n))
    prompt = prompt.replace("{structure}", json.dumps(structure, indent=2, ensure_ascii=False))
    prompt = prompt.replace("{git_log}", git_log)

    if len(prompt) > 80_000:
        prompt = prompt[:80_000] + "\n... (truncated)"

    if verbose:
        print(f"  Prompt size: {len(prompt)} chars", file=sys.stderr)

    raw = _call_claude(prompt)
    modifications = _parse_json_response(raw)

    if isinstance(modifications, dict):
        modifications = modifications.get("modifications", [modifications])
    if not isinstance(modifications, list):
        modifications = []

    # Assign IDs if missing
    for i, mod in enumerate(modifications, 1):
        if "id" not in mod:
            mod["id"] = i

    if verbose:
        print(f"  Generated {len(modifications)} modifications", file=sys.stderr)
        for mod in modifications[:5]:
            cat = mod.get("category", "?")
            desc = mod.get("description", "?")[:60]
            print(f"    [{cat}] {mod.get('file', '?')}: {desc}", file=sys.stderr)
        if len(modifications) > 5:
            print(f"    ... and {len(modifications) - 5} more", file=sys.stderr)

    return modifications


# ---------------------------------------------------------------------------
# Step 2: Run scan on each modification
# ---------------------------------------------------------------------------

MAX_RETRIES = 1  # Retry failed scans once


def _scan_one(
    mod: dict,
    index: int,
    total: int,
    repo_path: str,
    backend: str,
    verbose: bool,
) -> dict:
    """Scan a single virtual modification. Thread-safe. Retries on failure."""
    target_file = mod.get("file", "")
    affected = mod.get("affected_files", [])
    description = mod.get("description", "")

    if verbose:
        print(f"[step 2] [{index}/{total}] Scanning: {target_file}", file=sys.stderr)

    scan_files = []
    if target_file:
        scan_files.append(target_file)
    for af in affected:
        if af not in scan_files:
            scan_files.append(af)

    if not scan_files:
        if verbose:
            print(f"  [{index}/{total}] Skipped (no files)", file=sys.stderr)
        return {"modification": mod, "findings": []}

    last_error = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            context = build_context(repo_path, scan_files)

            mod_context = (
                f"VIRTUAL MODIFICATION (stress-test):\n"
                f"File: {target_file}\n"
                f"Function: {mod.get('function', 'N/A')}\n"
                f"Change: {description}\n"
                f"Category: {mod.get('category', 'N/A')}\n\n"
                f"Analyze the code below assuming this modification has been made. "
                f"Look for structural contradictions that would arise FROM this change."
            )
            context.target_files.insert(0, FileContext(
                path="[virtual-modification]",
                content=mod_context,
                is_target=True,
            ))

            findings = detect(
                context,
                repo_name=Path(repo_path).name,
                backend=backend,
            )
            findings = [f for f in findings if not f.get("parse_error")]

            if verbose:
                print(f"  [{index}/{total}] Found {len(findings)} contradiction(s)", file=sys.stderr)

            return {"modification": mod, "findings": findings}

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                if verbose:
                    print(f"  [{index}/{total}] Retry ({e})", file=sys.stderr)
            else:
                if verbose:
                    print(f"  [{index}/{total}] Failed: {e}", file=sys.stderr)

    return {"modification": mod, "findings": [], "error": str(last_error)}


def run_scans(
    modifications: list[dict],
    repo_path: str,
    backend: str = "cli",
    verbose: bool = False,
    parallel: int = 1,
    on_result: "callable | None" = None,
) -> list[dict]:
    """Run scan on each virtual modification using existing detect engine.

    Args:
        parallel: Number of concurrent scans (default: 1 = sequential)
        on_result: Optional callback(result, index) called after each scan completes.
                   Enables incremental saving so partial progress survives interruptions.
    """
    total = len(modifications)

    if parallel <= 1:
        results = []
        for i, mod in enumerate(modifications, 1):
            r = _scan_one(mod, i, total, repo_path, backend, verbose)
            results.append(r)
            if on_result:
                on_result(r, i - 1)
        return results

    # Parallel execution via ThreadPoolExecutor
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if verbose:
        print(f"[step 2] Running {total} scans with {parallel} workers", file=sys.stderr)

    PER_SCAN_TIMEOUT = 600  # 10 minutes (increased to match claude -p timeout)

    results = [None] * total
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {
            pool.submit(_scan_one, mod, i, total, repo_path, backend, verbose): i - 1
            for i, mod in enumerate(modifications, 1)
        }
        for future in as_completed(futures, timeout=PER_SCAN_TIMEOUT * total):
            idx = futures[future]
            try:
                results[idx] = future.result(timeout=PER_SCAN_TIMEOUT)
            except TimeoutError:
                results[idx] = {"modification": modifications[idx], "findings": [], "error": "timeout"}
                if verbose:
                    print(f"[timeout] scan {idx+1}/{total} timed out after {PER_SCAN_TIMEOUT}s", file=sys.stderr)
            except Exception as exc:
                results[idx] = {"modification": modifications[idx], "findings": [], "error": str(exc)}
                if verbose:
                    print(f"[error] scan {idx+1}/{total}: {exc}", file=sys.stderr)
            if on_result:
                on_result(results[idx], idx)

    return [r if r is not None else {"modification": modifications[i], "findings": [], "error": "incomplete"} for i, r in enumerate(results)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_claude(prompt: str) -> str:
    """Call claude -p (subscription CLI, $0 cost)."""
    try:
        result = subprocess.run(
            ["claude", "-p"],
            input=prompt,
            capture_output=True, text=True, timeout=900,  # 15 minutes (increased from 10)
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude -p failed: {result.stderr[:300]}")
        return result.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude -p timed out after 15 minutes")


def _parse_json_response(raw: str) -> dict | list:
    """Parse JSON from LLM response, handling markdown code blocks."""
    text = raw.strip()

    # Extract from markdown code block
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object or array
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

    return {}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

BATCH_SIZE = 10  # Save results every N scans
CONVERGENCE_WINDOW = 20  # Check convergence over last N scans
MIN_SCANS_BEFORE_CONVERGENCE = 30  # Don't stop before this many


def estimate_n(n_source_files: int) -> int:
    """Auto-determine modification count based on repo size.

    Heuristic: aim for ~20% coverage of source files, clamped to [20, 300].
    """
    n = max(20, min(int(n_source_files * 0.2), 300))
    # Round to nearest 10
    return ((n + 5) // 10) * 10


def _check_convergence(results: list[dict], verbose: bool) -> bool:
    """Check if the risk map has converged (no new files discovered recently).

    Returns True if we should stop scanning.
    """
    if len(results) < MIN_SCANS_BEFORE_CONVERGENCE:
        return False

    # Files discovered in the earlier portion
    early = results[:-CONVERGENCE_WINDOW]
    recent = results[-CONVERGENCE_WINDOW:]

    early_files: set[str] = set()
    for r in early:
        mod = r.get("modification", {})
        if r.get("findings"):
            f = mod.get("file", "")
            if f:
                early_files.add(f)
            for af in mod.get("affected_files", []):
                early_files.add(af)

    new_files = 0
    for r in recent:
        mod = r.get("modification", {})
        if r.get("findings"):
            f = mod.get("file", "")
            if f and f not in early_files:
                new_files += 1
            for af in mod.get("affected_files", []):
                if af not in early_files:
                    new_files += 1

    if verbose:
        print(f"  [convergence] {new_files} new files in last {CONVERGENCE_WINDOW} scans", file=sys.stderr)

    return new_files == 0


def _get_hotspot_summary(results: list[dict], n_top: int = 10) -> str:
    """Build a hotspot summary string from current results for focused generation."""
    from collections import Counter
    file_hits = Counter()
    for r in results:
        if not r.get("findings"):
            continue
        mod = r.get("modification", {})
        f = mod.get("file", "")
        if f:
            file_hits[f] += len(r["findings"])
        for af in mod.get("affected_files", []):
            file_hits[af] += 1

    lines = []
    for f, count in file_hits.most_common(n_top):
        lines.append(f"- {f}: {count} findings")
    return "\n".join(lines) if lines else "No hotspots identified yet."


def _get_tested_summary(results: list[dict]) -> str:
    """Build summary of already-tested modifications to avoid repetition."""
    lines = []
    for r in results:
        mod = r.get("modification", {})
        f = mod.get("file", "")
        desc = mod.get("description", "")[:80]
        lines.append(f"- {f}: {desc}")
    return "\n".join(lines[-30:])  # Last 30 to keep prompt size reasonable


def generate_focused_modifications(
    structure: dict,
    results: list[dict],
    repo_path: str,
    n: int = 10,
    verbose: bool = False,
) -> list[dict]:
    """Generate focused modifications targeting discovered hotspots."""
    if verbose:
        print(f"[adaptive] Generating {n} focused modifications on hotspots...", file=sys.stderr)

    hotspots = _get_hotspot_summary(results)
    already_tested = _get_tested_summary(results)

    prompt_template = (PROMPT_DIR / "generate_focused_modifications.md").read_text(encoding="utf-8")
    prompt = prompt_template.replace("{n}", str(n))
    prompt = prompt.replace("{structure}", json.dumps(structure, indent=2, ensure_ascii=False))
    prompt = prompt.replace("{hotspots}", hotspots)
    prompt = prompt.replace("{already_tested}", already_tested)

    if len(prompt) > 80_000:
        prompt = prompt[:80_000] + "\n... (truncated)"

    raw = _call_claude(prompt)
    modifications = _parse_json_response(raw)

    if isinstance(modifications, dict):
        modifications = modifications.get("modifications", [modifications])
    if not isinstance(modifications, list):
        modifications = []

    # Assign IDs continuing from current count
    base_id = len(results) + 1
    for i, mod in enumerate(modifications):
        mod["id"] = base_id + i
        mod.setdefault("category", "focused")

    if verbose:
        print(f"  Generated {len(modifications)} focused modifications", file=sys.stderr)

    return modifications


def _save_results(out: Path, results: list[dict], metadata: dict, verbose: bool):
    """Save current results to results.json (incremental update)."""
    results_path = out / "results.json"
    output_data = {
        "metadata": {**metadata, "n_completed": len(results)},
        "results": results,
    }
    results_path.write_text(
        json.dumps(output_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        total_findings = sum(len(r.get("findings", [])) for r in results)
        hit_mods = sum(1 for r in results if r.get("findings"))
        print(f"  [checkpoint] {len(results)} scans, {hit_mods} hits, {total_findings} findings", file=sys.stderr)


def _update_heatmap(out: Path, verbose: bool):
    """Regenerate unified dashboard with treemap from current results.json."""
    try:
        from visualize import build_treemap_json
        from findings import generate_dashboard

        results_path = out / "results.json"
        if not results_path.exists():
            return
        treemap = build_treemap_json(str(results_path))
        # repo root is two levels up from .delta-lint/stress-test/
        repo_path = out.parent.parent
        generate_dashboard(str(repo_path), treemap_json=treemap)
    except Exception as e:
        if verbose:
            print(f"  [dashboard] update failed: {e}", file=sys.stderr)


def run_stress_test(
    repo_path: str,
    n_modifications: int = 0,
    backend: str = "cli",
    verbose: bool = False,
    output_dir: str | None = None,
    parallel: int = 1,
    visualize: bool = True,
    lang: str = "en",
    structure: dict | None = None,
    skip_existing: bool = False,
    max_wall_time: int = 2400,
) -> list[dict]:
    """Main entry point — autonomous adaptive stress-test.

    Autonomy features:
    - n=0 (default): auto-determines count from repo size
    - Incremental saves every BATCH_SIZE (10) scans
    - After initial batch, generates focused modifications targeting hotspots
    - Auto-converges when no new files are discovered
    - Retries failed scans automatically

    Args:
        structure: Pre-computed structure dict from init_lightweight/analyze_structure.
                   If provided, skips redundant structure analysis.
        skip_existing: If True, skip the scan_existing step (Step 0.5).
                       Use when caller already runs scan_existing in parallel.
        max_wall_time: Maximum wall-clock seconds before graceful shutdown (default 2400 = 40min).
    """
    repo_path = str(Path(repo_path).resolve())
    if output_dir is None:
        output_dir = str(Path(repo_path) / ".delta-lint" / "stress-test")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Auto-generate .gitignore in .delta-lint/ (self-contained, no root .gitignore edit)
    delta_lint_dir = out.parent  # .delta-lint/
    gitignore_path = delta_lint_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(
            "# delta-lint generated data (ignored by default)\n"
            "# To share landmine map with team, remove lines below and commit\n"
            "*\n"
            "!.gitignore\n"
        )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Step 0: Structure analysis (skip if pre-computed)
    if structure is None:
        structure = analyze_structure(repo_path, verbose=verbose)
    structure_path = out / "structure.json"
    structure_path.write_text(
        json.dumps(structure, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        print(f"  Saved: {structure_path}", file=sys.stderr)

    # Step 0.5: Scan existing contradictions in hotspot clusters
    existing_results: list[dict] = []
    if not skip_existing:
        repo_name = Path(repo_path).name
        n_saved_existing = 0
        for result, completed, total in scan_existing(
            structure, repo_path,
            backend=backend, verbose=verbose, parallel=parallel, lang=lang,
            stream=True,
        ):
            existing_results.append(result)
            # Save each finding to JSONL immediately (survives interruption)
            try:
                from findings import Finding, generate_id, add_finding
                for f in result.get("findings", []):
                    loc = f.get("location", {})
                    file_a = loc.get("file_a", "") if isinstance(loc, dict) else ""
                    file_b = loc.get("file_b", "") if isinstance(loc, dict) else ""
                    pattern = f.get("pattern", "")
                    title = f.get("contradiction", f.get("title", ""))[:120]
                    fid = generate_id(repo_name, file_a, title,
                                      file_b=file_b, pattern=pattern)
                    try:
                        from git_enrichment import enrich_finding
                        enrich_finding(f, repo_path)
                    except Exception:
                        pass
                    finding = Finding(
                        id=fid, repo=repo_name, file=file_a,
                        severity=f.get("severity", "medium"),
                        pattern=pattern, title=title,
                        description=f.get("impact", f.get("user_impact", "")),
                        category=f.get("category", "contradiction"),
                        found_by="delta-init",
                        churn_6m=f.get("churn_6m", 0),
                        fan_out=f.get("fan_out", 0),
                        total_lines=f.get("total_lines", 0),
                    )
                    try:
                        add_finding(repo_path, finding)
                        n_saved_existing += 1
                    except ValueError:
                        pass
            except Exception:
                pass

        if verbose and n_saved_existing:
            print(f"  [existing] {n_saved_existing} findings saved to JSONL", file=sys.stderr)

        existing_findings_path = out / "existing_findings.json"
        existing_data = {
            "metadata": {
                "repo": repo_path,
                "repo_name": repo_name,
                "timestamp": timestamp,
                "n_clusters": len(existing_results),
            },
            "results": existing_results,
        }
        existing_findings_path.write_text(
            json.dumps(existing_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if verbose:
            n_findings = sum(len(r.get("findings", [])) for r in existing_results)
            n_hits = sum(1 for r in existing_results if r.get("findings"))
            print(f"  Saved: {existing_findings_path}", file=sys.stderr)
            print(f"  {n_hits}/{len(existing_results)} clusters had existing contradictions ({n_findings} total)", file=sys.stderr)

    # Auto-determine n from repo size if not specified
    if n_modifications <= 0:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        n_source = len(filter_source_files(result.stdout.strip().split("\n")))
        n_modifications = estimate_n(n_source)
        if verbose:
            print(f"[auto] {n_source} source files → n={n_modifications} modifications", file=sys.stderr)

    metadata = {
        "repo": repo_path,
        "repo_name": Path(repo_path).name,
        "n_modifications": n_modifications,
        "timestamp": timestamp,
        "backend": backend,
    }

    # Step 1: Generate initial modifications
    initial_n = min(n_modifications, BATCH_SIZE * 3)  # First 30 from broad generation
    modifications = generate_modifications(
        structure, repo_path, n=initial_n, verbose=verbose,
    )
    mods_path = out / "modifications.json"
    mods_path.write_text(
        json.dumps(modifications, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verbose:
        print(f"  Saved: {mods_path}", file=sys.stderr)

    # Step 2: Adaptive scan loop
    all_results: list[dict] = []
    pending = list(modifications)
    converged = False
    _last_save_count = 0
    _wall_start = _time.monotonic()

    def _on_scan_result(result, idx):
        nonlocal _last_save_count
        all_results.append(result)
        metadata["n_completed"] = len(all_results)
        _save_results(out, all_results, metadata, verbose=False)
        _last_save_count = len(all_results)

    while len(all_results) < n_modifications and not converged:
        elapsed = _time.monotonic() - _wall_start
        if elapsed > max_wall_time:
            if verbose:
                print(f"[wall-time] {elapsed:.0f}s elapsed (limit {max_wall_time}s). "
                      f"Stopping with {len(all_results)}/{n_modifications} scans.", file=sys.stderr)
            metadata["status"] = "timeout"
            _save_results(out, all_results, metadata, verbose=False)
            break
        # Take next batch from pending
        batch = pending[:BATCH_SIZE]
        pending = pending[BATCH_SIZE:]

        if not batch:
            remaining = n_modifications - len(all_results)
            focus_n = min(BATCH_SIZE, remaining)
            if focus_n <= 0:
                break
            batch = generate_focused_modifications(
                structure, all_results, repo_path, n=focus_n, verbose=verbose,
            )
            if not batch:
                if verbose:
                    print("[adaptive] No more modifications to generate. Stopping.", file=sys.stderr)
                break

        pre_count = len(all_results)
        run_scans(
            batch, repo_path, backend=backend, verbose=verbose, parallel=parallel,
            on_result=_on_scan_result,
        )

        if verbose:
            added = len(all_results) - pre_count
            total_findings = sum(len(r.get("findings", [])) for r in all_results)
            print(f"  [batch] +{added} scans ({len(all_results)} total), {total_findings} findings", file=sys.stderr)

        if visualize:
            _update_heatmap(out, verbose)

        if _check_convergence(all_results, verbose):
            converged = True
            if verbose:
                print(f"[adaptive] Converged at {len(all_results)} scans. Map is stable.", file=sys.stderr)

    # Final summary
    total_findings = sum(len(r.get("findings", [])) for r in all_results)
    hit_mods = sum(1 for r in all_results if r.get("findings"))

    # Update progressive scan coverage
    coverage = update_coverage_from_results(repo_path, all_results, existing_results)
    n_covered = len(coverage.get("scanned_files", {}))
    n_total = len(_list_source_files(repo_path))
    coverage_pct = round(n_covered / max(n_total, 1) * 100)

    # Record stress test in scan_history.jsonl
    elapsed_sec = _time.monotonic() - _wall_start
    try:
        from findings import append_scan_history
        append_scan_history(
            repo_path,
            clusters=len(all_results),
            findings_count=total_findings,
            duration_sec=elapsed_sec,
            scan_type="stress",
            scope="wide",
            depth="default",
            lens="stress",
        )
    except Exception:
        pass

    if verbose:
        status = "converged" if converged else "completed"
        print(f"\n[summary] {status} after {len(all_results)} scans", file=sys.stderr)
        print(f"[summary] {hit_mods}/{len(all_results)} modifications triggered contradictions", file=sys.stderr)
        print(f"[summary] {total_findings} total findings", file=sys.stderr)
        print(f"[summary] Coverage: {n_covered}/{n_total} files ({coverage_pct}%) — scan #{coverage['total_scans']}", file=sys.stderr)
        print(f"  Saved: {out / 'results.json'}", file=sys.stderr)

    return all_results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="delta-lint stress-test: generate virtual modifications and scan for contradictions"
    )
    parser.add_argument("--repo", required=True, help="Repository path")
    parser.add_argument("--n", type=int, default=0, help="Number of modifications (0=auto-determine from repo size)")
    parser.add_argument("--backend", default="cli", choices=["cli", "api"], help="LLM backend (default: cli = $0)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print progress to stderr")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    parser.add_argument("--parallel", type=int, default=1, help="Concurrent scans (default: 1, recommended max: 10)")
    parser.add_argument("--visualize", action="store_true", default=True, help="Generate HTML heatmap after scan (default: on)")
    parser.add_argument("--no-visualize", action="store_true", help="Disable HTML heatmap generation")
    parser.add_argument("--lang", default="en", choices=["en", "ja"], help="Output language for findings (default: en)")
    parser.add_argument("--max-wall-time", type=int, default=2400, help="Max wall-clock seconds before graceful stop (default: 2400 = 40min)")
    parser.add_argument("--structure-only", action="store_true", help="Run only structure analysis (Step 0), then exit")

    args = parser.parse_args()

    if args.structure_only:
        structure = analyze_structure(args.repo, verbose=args.verbose)
        output_dir = Path(args.output_dir or Path(args.repo) / ".delta-lint" / "stress-test")
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / "structure.json", "w") as f:
            json.dump(structure, f, indent=2, ensure_ascii=False)
        # Print summary for immediate display
        modules = structure.get("modules", [])
        hotspots = structure.get("hotspots", [])
        constraints = structure.get("implicit_constraints", [])
        print(f"modules: {len(modules)}")
        print(f"hotspots: {len(hotspots)}")
        for h in hotspots[:5]:
            print(f"  hotspot: {h.get('path', h.get('file', ''))} — {h.get('reason', '')}")
        for c in constraints[:5]:
            print(f"  constraint: {c}")
        sys.exit(0)

    results = run_stress_test(
        repo_path=args.repo,
        n_modifications=args.n,
        backend=args.backend,
        verbose=args.verbose,
        output_dir=args.output_dir,
        parallel=args.parallel,
        visualize=args.visualize and not args.no_visualize,
        lang=args.lang,
        max_wall_time=args.max_wall_time,
    )

    # Summary output
    total_findings = sum(len(r.get("findings", [])) for r in results)
    hit_mods = sum(1 for r in results if r.get("findings"))
    print(f"{hit_mods}/{len(results)} modifications triggered {total_findings} findings")
