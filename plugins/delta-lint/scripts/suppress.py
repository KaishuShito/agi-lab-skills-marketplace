"""
Suppress mechanism for delta-lint.

Responsible for:
- Computing stable finding_hash (LLM-output-independent)
- Computing code_hash for auto-expiration
- Loading/saving suppress.yml
- Matching findings against suppressions

Design decisions:
- finding_hash uses sorted files + rounded line numbers (NOT pattern_id, NOT LLM text)
- file_a/file_b are sorted to ensure order-independence
- Line numbers are rounded to 5-line buckets to absorb LLM variance of ±4 lines
- Falls back to files-only hash when line numbers are unavailable
- code_hash uses ±10 lines around the target for expiration detection
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

WHY_TYPES = {"domain", "technical", "preference"}
WHY_TYPE_SHORTCUTS = {"d": "domain", "t": "technical", "p": "preference"}
MIN_WHY_LENGTH_EN = 20
MIN_WHY_LENGTH_JA = 10

SUPPRESS_FILENAME = "suppress.yml"
CODE_HASH_RADIUS = 10  # ±10 lines for code_hash
LINE_ROUND_UNIT = 5    # round line numbers to this unit


@dataclass
class SuppressEntry:
    id: str                          # = finding_hash (first 8 hex chars)
    finding_hash: str                # hash of sorted_files + rounded_lines
    pattern: str                     # metadata only, NOT used for matching
    files: list[str]                 # sorted [file_a, file_b]
    code_hash: str                   # hash of surrounding code at suppress time
    why: str
    why_type: str                    # domain | technical | preference
    date: str
    author: str
    line_ranges: list[str] = field(default_factory=list)  # optional
    approved_by: str = ""            # 承認者（空 = 未承認 = 自己判断）


# ---------------------------------------------------------------------------
# Hash computation
# ---------------------------------------------------------------------------

def compute_finding_hash(finding: dict) -> str:
    """Compute a stable hash for a finding, independent of LLM output text.

    Hash is based on sorted file paths + rounded line numbers.
    Falls back to files-only when line numbers are unavailable.
    """
    loc = finding.get("location", {})
    file_a = loc.get("file_a", "")
    file_b = loc.get("file_b", "")

    # Sort files for order-independence
    if file_a <= file_b:
        files = [file_a, file_b]
        detail_first = loc.get("detail_a", "")
        detail_second = loc.get("detail_b", "")
    else:
        files = [file_b, file_a]
        detail_first = loc.get("detail_b", "")
        detail_second = loc.get("detail_a", "")

    line_first = _extract_line_number(detail_first)
    line_second = _extract_line_number(detail_second)

    if line_first is not None and line_second is not None:
        rounded_first = _round_line(line_first)
        rounded_second = _round_line(line_second)
        hash_input = f"{files[0]}:{rounded_first}:{files[1]}:{rounded_second}"
    else:
        # Fallback: files only (coarser but stable)
        hash_input = f"{files[0]}:{files[1]}"

    return hashlib.sha256(hash_input.encode()).hexdigest()[:8]


def compute_code_hash(repo_path: str, filepath: str,
                      line_number: Optional[int] = None) -> str:
    """Compute hash of code surrounding the target location.

    Uses ±CODE_HASH_RADIUS lines around the target line.
    Falls back to full file hash if line_number is unavailable.
    """
    full_path = Path(repo_path) / filepath
    if not full_path.exists():
        return "missing"

    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "unreadable"

    if line_number is not None and 1 <= line_number <= len(lines):
        # 0-indexed
        idx = line_number - 1
        start = max(0, idx - CODE_HASH_RADIUS)
        end = min(len(lines), idx + CODE_HASH_RADIUS + 1)
        snippet = "\n".join(lines[start:end])
    else:
        # Fallback: full file
        snippet = "\n".join(lines)

    return hashlib.sha256(snippet.encode()).hexdigest()[:8]


def _round_line(line: int) -> int:
    """Round line number to LINE_ROUND_UNIT bucket."""
    return (line // LINE_ROUND_UNIT) * LINE_ROUND_UNIT


def _extract_line_number(detail: str) -> Optional[int]:
    """Extract line number from LLM detail string.

    Handles formats like:
    - "line ~42"
    - "line 42"
    - "L42"
    - "lines 40-50" (takes first number)
    - "function foo(), line ~42: `code`"

    Returns None if no line number found.
    """
    if not detail:
        return None
    match = re.search(r'(?:lines?\s*~?\s*|L)(\d+)', detail, re.IGNORECASE)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Suppress file I/O
# ---------------------------------------------------------------------------

def _get_suppress_path(repo_path: str) -> Path:
    return Path(repo_path) / ".delta-lint" / SUPPRESS_FILENAME


def load_suppressions(repo_path: str) -> list[SuppressEntry]:
    """Load suppress entries from .delta-lint/suppress.yml."""
    path = _get_suppress_path(repo_path)
    if not path.exists():
        return []

    raw = path.read_text(encoding="utf-8")

    if yaml is not None:
        data = yaml.safe_load(raw)
    else:
        # Minimal YAML fallback: parse as JSON (suppress.yml is simple enough)
        import json
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

    if not isinstance(data, list):
        return []

    entries = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            entry = SuppressEntry(
                id=str(item.get("id", "")),
                finding_hash=str(item.get("finding_hash", "")),
                pattern=str(item.get("pattern", "")),
                files=list(item.get("files", [])),
                code_hash=str(item.get("code_hash", "")),
                why=str(item.get("why", "")),
                why_type=str(item.get("why_type", "")),
                date=str(item.get("date", "")),
                author=str(item.get("author", "")),
                line_ranges=list(item.get("line_ranges", [])),
                approved_by=str(item.get("approved_by", "")),
            )
            entries.append(entry)
        except (TypeError, ValueError):
            continue

    return entries


def save_suppressions(repo_path: str, entries: list[SuppressEntry]) -> Path:
    """Save suppress entries to .delta-lint/suppress.yml."""
    path = _get_suppress_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = []
    for e in entries:
        item = {
            "id": e.id,
            "finding_hash": e.finding_hash,
            "pattern": e.pattern,
            "files": e.files,
            "code_hash": e.code_hash,
            "why": e.why,
            "why_type": e.why_type,
            "date": e.date,
            "author": e.author,
        }
        if e.line_ranges:
            item["line_ranges"] = e.line_ranges
        if e.approved_by:
            item["approved_by"] = e.approved_by
        data.append(item)

    if yaml is not None:
        content = yaml.dump(data, default_flow_style=False,
                            allow_unicode=True, sort_keys=False)
    else:
        import json
        content = json.dumps(data, indent=2, ensure_ascii=False)

    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_finding(finding: dict,
                  suppressions: list[SuppressEntry],
                  repo_path: str) -> tuple[Optional[SuppressEntry], bool]:
    """Match a finding against suppress entries.

    Returns:
        (entry, expired):
        - (None, False)  — no matching suppress
        - (entry, False) — matched and still valid
        - (entry, True)  — matched but code_hash changed (expired)
    """
    fhash = compute_finding_hash(finding)

    for entry in suppressions:
        if entry.finding_hash == fhash:
            # Check code_hash for expiration
            loc = finding.get("location", {})
            file_a = loc.get("file_a", "")
            line_a = _extract_line_number(loc.get("detail_a", ""))
            current_hash = compute_code_hash(repo_path, file_a, line_a)

            if current_hash != entry.code_hash:
                return entry, True  # expired
            return entry, False  # valid suppress

    return None, False


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_why(text: str) -> Optional[str]:
    """Validate the 'why' field. Returns error message or None if valid."""
    if not text or not text.strip():
        return "why is required"

    stripped = text.strip()

    # Detect Japanese characters
    has_japanese = bool(re.search(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', stripped))

    if has_japanese:
        if len(stripped) < MIN_WHY_LENGTH_JA:
            return f"why must be at least {MIN_WHY_LENGTH_JA} characters (Japanese)"
    else:
        if len(stripped) < MIN_WHY_LENGTH_EN:
            return f"why must be at least {MIN_WHY_LENGTH_EN} characters (English)"

    return None


def validate_why_type(value: str) -> Optional[str]:
    """Validate why_type. Returns error message or None if valid."""
    resolved = WHY_TYPE_SHORTCUTS.get(value.lower(), value.lower())
    if resolved not in WHY_TYPES:
        return f"why_type must be one of: {', '.join(sorted(WHY_TYPES))}"
    return None


def resolve_why_type(value: str) -> str:
    """Resolve why_type shortcut to full name."""
    return WHY_TYPE_SHORTCUTS.get(value.lower(), value.lower())
