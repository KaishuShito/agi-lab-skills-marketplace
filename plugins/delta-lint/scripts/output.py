"""
Output layer for delta-lint MVP.

Responsible for:
- Filtering findings by severity
- Suppress matching (finding_hash) and expiration (code_hash)
- Formatting as JSON or Markdown
- Logging all findings (including filtered/suppressed ones)

Design decisions:
- LLM outputs all findings; filtering happens HERE (not in prompt)
- This avoids detection suppression bias (Experiment 1b insight)
- severity: high is shown by default; medium/low go to log
- suppress: finding_hash match → hide; code_hash mismatch → expired (re-show + warn)
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from suppress import SuppressEntry, match_finding


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class FilterResult:
    shown: list[dict] = field(default_factory=list)
    filtered: list[dict] = field(default_factory=list)
    suppressed: list[dict] = field(default_factory=list)
    expired: list[dict] = field(default_factory=list)
    expired_entries: list[SuppressEntry] = field(default_factory=list)


def filter_findings(findings: list[dict], min_severity: str = "high",
                    suppressions: Optional[list[SuppressEntry]] = None,
                    repo_path: Optional[str] = None) -> FilterResult:
    """Split findings into shown, filtered, suppressed, and expired.

    Args:
        findings: Raw findings from detector
        min_severity: Minimum severity to show ("high", "medium", "low")
        suppressions: Loaded suppress entries (None = no suppress)
        repo_path: Repository root (needed for code_hash expiration check)

    Returns:
        FilterResult with 4 categories
    """
    result = FilterResult()
    threshold = SEVERITY_ORDER.get(min_severity, 0)
    suppressions = suppressions or []

    for f in findings:
        if f.get("parse_error"):
            result.shown.append(f)
            continue

        # Step 1: suppress matching
        if suppressions and repo_path:
            entry, expired = match_finding(f, suppressions, repo_path)
            if entry is not None:
                if expired:
                    # Code changed → suppress expired → re-show with warning
                    f["_expired_suppress"] = entry.id
                    result.expired.append(f)
                    result.expired_entries.append(entry)
                else:
                    # Valid suppress → hide
                    f["_suppress_id"] = entry.id
                    result.suppressed.append(f)
                continue

        # Step 2: severity filter
        sev = f.get("severity", "medium").lower()
        if SEVERITY_ORDER.get(sev, 1) <= threshold:
            result.shown.append(f)
        else:
            result.filtered.append(f)

    # Expired findings go to shown (they need attention)
    result.shown.extend(result.expired)

    # Sort shown by severity (high first)
    result.shown.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity", "medium").lower(), 1))
    return result


def format_json(findings: list[dict]) -> str:
    """Format findings as JSON."""
    return json.dumps(findings, indent=2, ensure_ascii=False)


def format_markdown(findings: list[dict], filtered_count: int = 0,
                    suppressed_count: int = 0, expired_count: int = 0) -> str:
    """Format findings as readable Markdown."""
    if not findings:
        if suppressed_count > 0:
            return f"No new contradictions. ({suppressed_count} suppressed)\n"
        return "No structural contradictions detected.\n"

    lines = [f"# delta-lint: {len(findings)} contradiction(s) found\n"]

    for i, f in enumerate(findings, 1):
        if f.get("parse_error"):
            lines.append(f"## Finding {i} (unparsed)")
            lines.append(f"```\n{f.get('raw_response', 'N/A')[:500]}\n```\n")
            continue

        pattern = f.get("pattern", "?")
        severity = f.get("severity", "?")
        severity_icon = {"high": "!!!", "medium": "..", "low": "."}.get(severity.lower(), "?")

        # Mark expired findings
        expired_tag = ""
        if f.get("_expired_suppress"):
            expired_tag = " [EXPIRED SUPPRESS]"

        lines.append(f"## [{severity_icon}] Finding {i}: Pattern {pattern} ({severity}){expired_tag}")
        lines.append("")

        if f.get("_expired_suppress"):
            lines.append(f"> Suppress `{f['_expired_suppress']}` expired: code has changed since suppression.")
            lines.append("")

        loc = f.get("location", {})
        if isinstance(loc, dict):
            lines.append(f"**File A**: `{loc.get('file_a', '?')}`")
            if loc.get("detail_a"):
                lines.append(f"  {loc['detail_a']}")
            lines.append(f"**File B**: `{loc.get('file_b', '?')}`")
            if loc.get("detail_b"):
                lines.append(f"  {loc['detail_b']}")
        lines.append("")

        if f.get("contradiction"):
            lines.append(f"**Contradiction**: {f['contradiction']}")
        if f.get("impact"):
            lines.append(f"**Impact**: {f['impact']}")
        lines.append("")

    # Footer summary
    footer_parts = []
    if filtered_count > 0:
        footer_parts.append(f"{filtered_count} lower-severity filtered")
    if suppressed_count > 0:
        footer_parts.append(f"{suppressed_count} suppressed")
    if expired_count > 0:
        footer_parts.append(f"{expired_count} expired (re-shown above)")

    if footer_parts:
        lines.append(f"---\n*{', '.join(footer_parts)}.*\n")

    return "\n".join(lines)


def filter_diff_only(findings: list[dict], changed_files: list[str]) -> list[dict]:
    """Keep only findings where at least one location file is in the diff.

    Used by --diff-only to focus on findings directly related to current changes.
    """
    if not changed_files:
        return findings

    # Normalize for flexible matching
    changed_set = set()
    for f in changed_files:
        changed_set.add(f)
        if f.startswith("./"):
            changed_set.add(f[2:])
        else:
            changed_set.add("./" + f)

    result = []
    for f in findings:
        if f.get("parse_error"):
            result.append(f)
            continue
        loc = f.get("location", {})
        file_a = loc.get("file_a", "")
        file_b = loc.get("file_b", "")
        # Keep if either file is in the diff
        if _path_in_set(file_a, changed_set) or _path_in_set(file_b, changed_set):
            result.append(f)
    return result


def _path_in_set(path: str, path_set: set[str]) -> bool:
    """Check if path matches any entry in the set."""
    if not path:
        return False
    if path in path_set:
        return True
    if path.startswith("./"):
        return path[2:] in path_set
    return "./" + path in path_set


def save_log(result: FilterResult, context_meta: dict, output_dir: str) -> Path:
    """Save full log (all findings including filtered/suppressed) to a JSON file.

    Returns path to the saved log file.
    """
    log_dir = Path(output_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"delta_lint_{timestamp}.json"

    log_data = {
        "timestamp": timestamp,
        "context": context_meta,
        "findings_shown": result.shown,
        "findings_filtered": result.filtered,
        "findings_suppressed": result.suppressed,
        "findings_expired": result.expired,
        "total_findings": (len(result.shown) + len(result.filtered)
                          + len(result.suppressed)),
        "shown_count": len(result.shown),
        "filtered_count": len(result.filtered),
        "suppressed_count": len(result.suppressed),
        "expired_count": len(result.expired),
    }

    log_path.write_text(json.dumps(log_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return log_path


def print_results(findings: list[dict], filtered_count: int = 0,
                  suppressed_count: int = 0, expired_count: int = 0,
                  output_format: str = "markdown", file=None):
    """Print formatted results to stdout or a file."""
    out = file or sys.stdout

    if output_format == "json":
        print(format_json(findings), file=out)
    else:
        print(format_markdown(findings, filtered_count,
                              suppressed_count, expired_count), file=out)
