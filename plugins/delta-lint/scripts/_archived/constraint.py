"""
Constraint store for delta-lint.

Manages external knowledge (business rules, API specs, design decisions)
that is not expressed in code but affects structural correctness.

Two modes:
- YAML (default): .delta-lint/constraints.yml — structural search only
- DB + vector (future): .delta-lint/knowledge.db — structural + semantic search

This module implements YAML mode.

Design decisions:
- Same file convention as suppress.yml (.delta-lint/ directory)
- Structural search = file_path matching against changed files
- Constraints are injected into the scan prompt alongside code context
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONSTRAINTS_FILENAME = "constraints.yml"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ConstraintEntry:
    id: str                          # unique id (e.g. "c001")
    content: str                     # the constraint text
    files: list[str] = field(default_factory=list)   # linked file paths
    source_type: str = ""            # slack, doc, meeting, etc.
    source_url: str = ""             # optional link to source
    created_at: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _get_constraints_path(repo_path: str) -> Path:
    return Path(repo_path) / ".delta-lint" / CONSTRAINTS_FILENAME


def _next_id(entries: list[ConstraintEntry]) -> str:
    """Generate next constraint ID like c001, c002, ..."""
    max_num = 0
    for e in entries:
        m = re.match(r"c(\d+)", e.id)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"c{max_num + 1:03d}"


def load_constraints(repo_path: str) -> list[ConstraintEntry]:
    """Load constraint entries from .delta-lint/constraints.yml."""
    path = _get_constraints_path(repo_path)
    if not path.exists():
        return []

    raw = path.read_text(encoding="utf-8")

    if yaml is not None:
        data = yaml.safe_load(raw)
    else:
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
            entry = ConstraintEntry(
                id=str(item.get("id", "")),
                content=str(item.get("content", "")),
                files=list(item.get("files", [])),
                source_type=str(item.get("source_type", "")),
                source_url=str(item.get("source_url", "")),
                created_at=str(item.get("created_at", "")),
                author=str(item.get("author", "")),
                tags=list(item.get("tags", [])),
            )
            entries.append(entry)
        except (TypeError, ValueError):
            continue

    return entries


def save_constraints(repo_path: str, entries: list[ConstraintEntry]) -> Path:
    """Save constraint entries to .delta-lint/constraints.yml."""
    path = _get_constraints_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = []
    for e in entries:
        item = {
            "id": e.id,
            "content": e.content,
        }
        if e.files:
            item["files"] = e.files
        if e.source_type:
            item["source_type"] = e.source_type
        if e.source_url:
            item["source_url"] = e.source_url
        if e.created_at:
            item["created_at"] = e.created_at
        if e.author:
            item["author"] = e.author
        if e.tags:
            item["tags"] = e.tags
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
# Search
# ---------------------------------------------------------------------------

def search_by_files(constraints: list[ConstraintEntry],
                    changed_files: list[str]) -> list[ConstraintEntry]:
    """Structural search: find constraints linked to any of the changed files.

    Matches if any constraint file path is a suffix of a changed file path,
    enabling both exact matches and partial path matches.
    """
    if not constraints or not changed_files:
        return []

    matched = []
    for c in constraints:
        if not c.files:
            continue
        for cf in c.files:
            for changed in changed_files:
                # Suffix match: "src/api-client.ts" matches "api-client.ts"
                if changed.endswith(cf) or cf.endswith(changed) or changed == cf:
                    matched.append(c)
                    break
            else:
                continue
            break

    return matched


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------

def constraints_to_prompt(constraints: list[ConstraintEntry]) -> str:
    """Format constraints for injection into the detection prompt.

    Returns empty string if no constraints.
    """
    if not constraints:
        return ""

    lines = [
        "\n## External Constraints (Knowledge Store)",
        "",
        "The following external constraints are NOT expressed in the code but "
        "MUST be respected. Check if any code changes violate these constraints:",
        "",
    ]

    for c in constraints:
        files_str = ", ".join(c.files) if c.files else "(no linked files)"
        lines.append(f"- **[{c.id}]** {c.content}")
        lines.append(f"  Linked files: {files_str}")
        if c.source_type:
            lines.append(f"  Source: {c.source_type}")
            if c.source_url:
                lines.append(f"  URL: {c.source_url}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------

def check_staleness(repo_path: str,
                    constraints: list[ConstraintEntry]) -> list[dict]:
    """Check for stale constraints (linked files no longer exist).

    Returns list of {constraint, missing_files} dicts.
    """
    stale = []
    repo = Path(repo_path)

    for c in constraints:
        missing = []
        for f in c.files:
            if not (repo / f).exists():
                missing.append(f)
        if missing:
            stale.append({"constraint": c, "missing_files": missing})

    return stale
