"""
Sibling map management for delta-lint.

Tracks known sibling code relationships (A↔B pairs with implicit contracts).
Used by retrieval.py to prioritize sibling files in context building.

Data format: .delta-lint/sibling_map.yml
See ナレッジ蓄積設計.md for full design.
"""

import hashlib
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class SiblingEntry:
    id: str
    file_a: str
    file_b: str
    contract: str = ""
    confidence: str = "medium"  # high / medium / low
    source: str = "finding"     # finding / structure_analysis / manual
    finding_id: str = ""
    discovered_at: str = ""
    last_verified: str = ""
    code_hash_a: str = ""
    code_hash_b: str = ""


def _pair_key(file_a: str, file_b: str) -> str:
    """Canonical key for a file pair (order-independent)."""
    return "|".join(sorted([file_a, file_b]))


def _generate_id(file_a: str, file_b: str, contract: str = "") -> str:
    """Generate a stable ID for a sibling entry."""
    key = _pair_key(file_a, file_b)
    h = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"s-{h}"


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def _sibling_map_path(repo_path: str) -> Path:
    return Path(repo_path) / ".delta-lint" / "sibling_map.yml"


def load_sibling_map(repo_path: str) -> list[SiblingEntry]:
    """Load sibling map from .delta-lint/sibling_map.yml."""
    if yaml is None:
        return []

    path = _sibling_map_path(repo_path)
    if not path.exists():
        return []

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(data, list):
        return []

    entries = []
    for item in data:
        if not isinstance(item, dict):
            continue
        entries.append(SiblingEntry(
            id=item.get("id", ""),
            file_a=item.get("file_a", ""),
            file_b=item.get("file_b", ""),
            contract=item.get("contract", ""),
            confidence=item.get("confidence", "medium"),
            source=item.get("source", "finding"),
            finding_id=item.get("finding_id", ""),
            discovered_at=item.get("discovered_at", ""),
            last_verified=item.get("last_verified", ""),
            code_hash_a=item.get("code_hash_a", ""),
            code_hash_b=item.get("code_hash_b", ""),
        ))
    return entries


def save_sibling_map(repo_path: str, entries: list[SiblingEntry]) -> Path:
    """Save sibling map to .delta-lint/sibling_map.yml."""
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")

    path = _sibling_map_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = []
    for e in entries:
        d = {
            "id": e.id,
            "file_a": e.file_a,
            "file_b": e.file_b,
            "contract": e.contract,
            "confidence": e.confidence,
            "source": e.source,
        }
        if e.finding_id:
            d["finding_id"] = e.finding_id
        if e.discovered_at:
            d["discovered_at"] = e.discovered_at
        if e.last_verified:
            d["last_verified"] = e.last_verified
        if e.code_hash_a:
            d["code_hash_a"] = e.code_hash_a
        if e.code_hash_b:
            d["code_hash_b"] = e.code_hash_b
        data.append(d)

    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def get_siblings(repo_path: str, changed_files: list[str]) -> list[str]:
    """Find sibling files for the given changed files.

    Returns file paths that are siblings of changed files but NOT in
    the changed files list themselves. Sorted by confidence (high first).
    """
    entries = load_sibling_map(repo_path)
    if not entries:
        return []

    # Normalize changed file paths for matching
    changed_set = set()
    for f in changed_files:
        changed_set.add(f)
        if f.startswith("./"):
            changed_set.add(f[2:])
        else:
            changed_set.add("./" + f)

    # Confidence ordering
    conf_order = {"high": 0, "medium": 1, "low": 2}

    siblings = []  # (confidence_rank, path)
    seen = set()

    for entry in entries:
        fa, fb = entry.file_a, entry.file_b
        conf_rank = conf_order.get(entry.confidence, 2)

        # Check if file_a is in changed → add file_b as sibling
        if _matches(fa, changed_set) and not _matches(fb, changed_set):
            if fb not in seen:
                siblings.append((conf_rank, fb))
                seen.add(fb)

        # Check if file_b is in changed → add file_a as sibling
        elif _matches(fb, changed_set) and not _matches(fa, changed_set):
            if fa not in seen:
                siblings.append((conf_rank, fa))
                seen.add(fa)

    # Sort by confidence (high first)
    siblings.sort(key=lambda x: x[0])
    return [path for _, path in siblings]


def _matches(file_path: str, file_set: set[str]) -> bool:
    """Check if file_path matches any path in the set (flexible matching)."""
    if file_path in file_set:
        return True
    # Try with/without ./ prefix
    if file_path.startswith("./"):
        return file_path[2:] in file_set
    return "./" + file_path in file_set


# ---------------------------------------------------------------------------
# Auto-extract from findings
# ---------------------------------------------------------------------------

def extract_siblings_from_findings(
    findings: list[dict],
    repo_path: str = "",
) -> list[SiblingEntry]:
    """Extract sibling relationships from scan findings.

    Each finding with file_a and file_b locations represents a potential
    sibling relationship. De-duplicates against existing sibling_map.
    """
    existing = load_sibling_map(repo_path) if repo_path else []
    existing_keys = {_pair_key(e.file_a, e.file_b) for e in existing}

    today = str(date.today())
    new_entries = []

    for f in findings:
        # Skip parse errors
        if f.get("parse_error"):
            continue

        loc = f.get("location", {})
        file_a = loc.get("file_a", "")
        file_b = loc.get("file_b", "")

        if not file_a or not file_b or file_a == file_b:
            continue

        key = _pair_key(file_a, file_b)
        if key in existing_keys:
            # Already tracked — update last_verified on existing
            for e in existing:
                if _pair_key(e.file_a, e.file_b) == key:
                    e.last_verified = today
                    break
            continue

        # New sibling relationship
        existing_keys.add(key)
        contract = f.get("contradiction", "")
        finding_id = f.get("id", "")

        new_entries.append(SiblingEntry(
            id=_generate_id(file_a, file_b),
            file_a=file_a,
            file_b=file_b,
            contract=contract[:200] if contract else "",
            confidence="medium",
            source="finding",
            finding_id=finding_id,
            discovered_at=today,
            last_verified=today,
        ))

    return new_entries


def update_sibling_map_from_findings(
    findings: list[dict],
    repo_path: str,
) -> int:
    """Extract siblings from findings and merge into sibling_map.yml.

    Returns the number of new entries added.
    """
    if yaml is None:
        return 0

    existing = load_sibling_map(repo_path)
    new_entries = extract_siblings_from_findings(findings, repo_path)

    if not new_entries and not any(True for _ in existing):
        return 0

    # Merge: existing (with updated last_verified) + new
    all_entries = existing + new_entries
    save_sibling_map(repo_path, all_entries)
    return len(new_entries)


# ---------------------------------------------------------------------------
# Git history-based sibling generation
# ---------------------------------------------------------------------------

def generate_siblings_from_git_history(
    repo_path: str,
    months: int = 6,
    min_co_changes: int = 3,
    max_pairs: int = 50,
    verbose: bool = False,
) -> list[SiblingEntry]:
    """Extract sibling relationships from git co-change history.

    Files that are frequently changed together in the same commit
    are likely siblings with implicit contracts between them.

    Args:
        repo_path: Repository root path
        months: How far back to look in git history
        min_co_changes: Minimum co-change count to consider a pair
        max_pairs: Maximum number of pairs to return
        verbose: Print progress to stderr

    Returns:
        List of new SiblingEntry objects (not yet saved)
    """
    import subprocess
    import sys
    from collections import Counter

    # Get commit-grouped file lists from git log
    try:
        result = subprocess.run(
            ["git", "log", f"--since={months} months ago",
             "--name-only", "--pretty=format:---COMMIT---"],
            capture_output=True, text=True, cwd=repo_path, timeout=30,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    # Parse commits → list of file sets
    commits: list[list[str]] = []
    current: list[str] = []
    for line in result.stdout.strip().split("\n"):
        if line == "---COMMIT---":
            if current:
                commits.append(current)
            current = []
        elif line.strip():
            current.append(line.strip())
    if current:
        commits.append(current)

    if verbose:
        print(f"  Git history: {len(commits)} commits in last {months} months", file=sys.stderr)

    # Filter to source files only (skip configs, docs, etc.)
    _SOURCE_EXTS = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
        ".php", ".rb", ".swift", ".kt", ".c", ".cpp", ".h", ".hpp",
        ".cs", ".vue", ".svelte",
    }

    def _is_source(f: str) -> bool:
        return Path(f).suffix.lower() in _SOURCE_EXTS

    # Count co-change pairs
    pair_count: Counter = Counter()
    for files in commits:
        src_files = [f for f in files if _is_source(f)]
        if len(src_files) < 2 or len(src_files) > 20:
            # Skip trivial or bulk commits
            continue
        # All pairs in this commit
        for i, a in enumerate(src_files):
            for b in src_files[i + 1:]:
                key = _pair_key(a, b)
                pair_count[key] += 1

    # Filter by minimum co-changes and sort by frequency
    frequent_pairs = [
        (key, count) for key, count in pair_count.most_common()
        if count >= min_co_changes
    ]

    if verbose:
        print(f"  Co-change pairs (>={min_co_changes}): {len(frequent_pairs)}", file=sys.stderr)

    # Build SiblingEntry list (de-duplicate against existing map)
    existing = load_sibling_map(repo_path)
    existing_keys = {_pair_key(e.file_a, e.file_b) for e in existing}

    today = str(date.today())
    new_entries: list[SiblingEntry] = []

    for key, count in frequent_pairs[:max_pairs]:
        if key in existing_keys:
            continue

        file_a, file_b = key.split("|")

        # Confidence based on co-change frequency
        if count >= 10:
            confidence = "high"
        elif count >= 5:
            confidence = "medium"
        else:
            confidence = "low"

        new_entries.append(SiblingEntry(
            id=_generate_id(file_a, file_b),
            file_a=file_a,
            file_b=file_b,
            contract=f"co-changed {count} times in last {months} months",
            confidence=confidence,
            source="git-history",
            discovered_at=today,
        ))

    return new_entries


def get_git_churn(
    repo_path: str,
    months: int = 6,
) -> list[dict]:
    """Get file change frequency from git history.

    Returns list of {path, changes, rank} sorted by change count (desc).
    Used to prioritize frequently modified files in stress test.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "log", f"--since={months} months ago",
             "--name-only", "--pretty=format:"],
            capture_output=True, text=True, cwd=repo_path, timeout=30,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    from collections import Counter
    file_count: Counter = Counter()
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line:
            file_count[line] += 1

    churn = []
    for rank, (path, count) in enumerate(file_count.most_common(), 1):
        churn.append({"path": path, "changes": count, "rank": rank})

    return churn
