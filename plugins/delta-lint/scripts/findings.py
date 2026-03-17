"""
Findings tracker for delta-lint.

JSONL-based append-only log of bugs, contradictions, and suspicious patterns
found across repositories. Designed for multi-LLM append workflows.

Storage: .delta-lint/findings/{repo_name}.jsonl
Each line is one JSON object (one finding).
Same-id entries = event log (latest line wins for status).
"""

import json
import hashlib
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


FINDINGS_DIR = ".delta-lint/findings"
INDEX_FILE = "_index.md"

# Valid values
VALID_TYPES = ("bug", "contradiction", "suspicious", "enhancement")
VALID_SEVERITIES = ("high", "medium", "low")
VALID_STATUSES = (
    "found",        # 発見したばかり
    "verified",     # コード確認済み
    "submitted",    # Issue/PR 提出済み
    "merged",       # PR マージ済み
    "rejected",     # メンテナに却下された
    "wontfix",      # 意図的な設計
    "duplicate",    # 既知の問題
)


@dataclass
class Finding:
    id: str
    repo: str
    file: str
    line: Optional[int] = None
    type: str = "bug"
    severity: str = "high"
    pattern: str = ""
    title: str = ""
    description: str = ""
    status: str = "found"
    github_url: str = ""
    found_by: str = ""
    found_at: str = ""
    verified: bool = False
    tags: list[str] | None = None


def _findings_dir(base_path: str | Path) -> Path:
    return Path(base_path) / FINDINGS_DIR


def _repo_file(base_path: str | Path, repo_name: str) -> Path:
    """Get JSONL file path for a repo. Sanitize name for filesystem."""
    safe_name = repo_name.replace("/", "__").replace("\\", "__")
    return _findings_dir(base_path) / f"{safe_name}.jsonl"


def generate_id(repo: str, file: str, title: str) -> str:
    """Generate a short deterministic ID from repo+file+title."""
    h = hashlib.sha256(f"{repo}:{file}:{title}".encode()).hexdigest()[:8]
    safe_repo = repo.split("/")[-1] if "/" in repo else repo
    return f"{safe_repo}-{h}"


def _load_lines(path: Path) -> list[dict]:
    """Load all JSONL lines, skipping malformed ones."""
    if not path.exists():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return lines


def _get_latest(lines: list[dict]) -> dict[str, dict]:
    """Collapse event log: for each id, keep the latest entry."""
    latest: dict[str, dict] = {}
    for entry in lines:
        fid = entry.get("id", "")
        if fid:
            latest[fid] = entry
    return latest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_finding(
    base_path: str | Path,
    finding: Finding,
) -> str:
    """Append a finding to the repo's JSONL file.

    Returns the finding ID.
    Raises ValueError if duplicate ID with same status exists.
    """
    base_path = Path(base_path)
    fdir = _findings_dir(base_path)
    fdir.mkdir(parents=True, exist_ok=True)

    fpath = _repo_file(base_path, finding.repo)

    # Check for exact duplicate (same id + same status)
    existing = _load_lines(fpath)
    latest = _get_latest(existing)
    if finding.id in latest and latest[finding.id].get("status") == finding.status:
        raise ValueError(f"Duplicate: {finding.id} already has status '{finding.status}'")

    # Set timestamp if not provided
    if not finding.found_at:
        finding.found_at = datetime.now().strftime("%Y-%m-%d")

    # Append
    data = asdict(finding)
    data["_updated_at"] = datetime.now().isoformat()
    with fpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")

    return finding.id


def _find_file_for_id(base_path: Path, finding_id: str) -> tuple[Path, dict] | None:
    """Search all JSONL files for a finding by ID. Returns (file_path, latest_entry) or None."""
    fdir = _findings_dir(base_path)
    if not fdir.exists():
        return None
    for fpath in sorted(fdir.glob("*.jsonl")):
        lines = _load_lines(fpath)
        latest = _get_latest(lines)
        if finding_id in latest:
            return fpath, latest[finding_id]
    return None


def update_status(
    base_path: str | Path,
    repo_name: str,
    finding_id: str,
    new_status: str,
    github_url: str = "",
) -> None:
    """Update a finding's status by appending a new event line."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {new_status}. Valid: {VALID_STATUSES}")

    base_path = Path(base_path)

    # If repo_name provided, look in that specific file; otherwise search all
    if repo_name:
        fpath = _repo_file(base_path, repo_name)
        lines = _load_lines(fpath)
        latest = _get_latest(lines)
        if finding_id not in latest:
            raise ValueError(f"Finding {finding_id} not found in {fpath}")
        entry = dict(latest[finding_id])
    else:
        result = _find_file_for_id(base_path, finding_id)
        if result is None:
            raise ValueError(f"Finding {finding_id} not found in any JSONL file")
        fpath, found_entry = result
        entry = dict(found_entry)

    # Update fields
    entry["status"] = new_status
    entry["_updated_at"] = datetime.now().isoformat()
    if github_url:
        entry["github_url"] = github_url

    with fpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def list_findings(
    base_path: str | Path,
    repo_name: str | None = None,
    status_filter: str | None = None,
    type_filter: str | None = None,
) -> list[dict]:
    """List findings (latest state per id).

    If repo_name is None, list across all repos.
    """
    base_path = Path(base_path)
    fdir = _findings_dir(base_path)

    if not fdir.exists():
        return []

    if repo_name:
        files = [_repo_file(base_path, repo_name)]
    else:
        files = sorted(fdir.glob("*.jsonl"))

    results = []
    for fpath in files:
        if not fpath.exists():
            continue
        lines = _load_lines(fpath)
        latest = _get_latest(lines)
        for entry in latest.values():
            if status_filter and entry.get("status") != status_filter:
                continue
            if type_filter and entry.get("type") != type_filter:
                continue
            results.append(entry)

    # Sort by severity (high first), then by found_at (newest first)
    sev_order = {"high": 0, "medium": 1, "low": 2}
    results.sort(key=lambda x: (
        sev_order.get(x.get("severity", "low"), 9),
        x.get("found_at", ""),
    ))
    return results


def search_findings(
    base_path: str | Path,
    query: str,
) -> list[dict]:
    """Search findings by keyword across all fields."""
    all_findings = list_findings(base_path)
    query_lower = query.lower()
    return [
        f for f in all_findings
        if query_lower in json.dumps(f, ensure_ascii=False).lower()
    ]


def get_stats(
    base_path: str | Path,
    repo_name: str | None = None,
) -> dict:
    """Get summary statistics."""
    findings = list_findings(base_path, repo_name=repo_name)

    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_repo: dict[str, int] = {}

    for f in findings:
        s = f.get("status", "unknown")
        t = f.get("type", "unknown")
        sev = f.get("severity", "unknown")
        r = f.get("repo", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
        by_type[t] = by_type.get(t, 0) + 1
        by_severity[sev] = by_severity.get(sev, 0) + 1
        by_repo[r] = by_repo.get(r, 0) + 1

    return {
        "total": len(findings),
        "by_status": by_status,
        "by_type": by_type,
        "by_severity": by_severity,
        "by_repo": by_repo,
    }


def generate_index(base_path: str | Path) -> str:
    """Generate _index.md content from all findings."""
    stats = get_stats(base_path)
    findings = list_findings(base_path)

    lines = [
        "# delta-lint Findings Index",
        "",
        f"**Total**: {stats['total']} findings",
        "",
    ]

    # Status summary
    lines.append("## Status")
    for status, count in sorted(stats["by_status"].items()):
        lines.append(f"- {status}: {count}")
    lines.append("")

    # By repo
    lines.append("## By Repository")
    for repo, count in sorted(stats["by_repo"].items(), key=lambda x: -x[1]):
        lines.append(f"- **{repo}**: {count}")
    lines.append("")

    # Finding list
    lines.append("## Findings")
    lines.append("")
    lines.append("| ID | Repo | File | Severity | Type | Status | Title |")
    lines.append("|-----|------|------|----------|------|--------|-------|")
    for f in findings:
        fid = f.get("id", "?")
        repo = f.get("repo", "?")
        file_ = f.get("file", "?")
        sev = f.get("severity", "?")
        typ = f.get("type", "?")
        status = f.get("status", "?")
        title = f.get("title", "?")
        url = f.get("github_url", "")
        title_cell = f"[{title}]({url})" if url else title
        lines.append(f"| {fid} | {repo} | {file_} | {sev} | {typ} | {status} | {title_cell} |")

    return "\n".join(lines) + "\n"


def save_index(base_path: str | Path) -> Path:
    """Generate and save _index.md."""
    base_path = Path(base_path)
    fdir = _findings_dir(base_path)
    fdir.mkdir(parents=True, exist_ok=True)
    index_path = fdir / INDEX_FILE
    index_path.write_text(generate_index(base_path), encoding="utf-8")
    return index_path


# ---------------------------------------------------------------------------
# CLI interface (called from cli.py)
# ---------------------------------------------------------------------------

def cmd_findings(args) -> None:
    """Handle findings subcommand."""
    base_path = str(Path(args.repo).resolve())

    if args.findings_command == "add":
        _findings_add(base_path, args)
    elif args.findings_command == "list":
        _findings_list(base_path, args)
    elif args.findings_command == "update":
        _findings_update(base_path, args)
    elif args.findings_command == "search":
        _findings_search(base_path, args)
    elif args.findings_command == "stats":
        _findings_stats(base_path, args)
    elif args.findings_command == "index":
        _findings_index(base_path, args)
    else:
        print("Usage: delta-lint findings {add|list|update|search|stats|index}", file=sys.stderr)
        sys.exit(1)


def _findings_add(base_path: str, args) -> None:
    repo_name = args.repo_name or Path(base_path).name
    fid = args.id or generate_id(repo_name, args.file or "", args.title or "")

    finding = Finding(
        id=fid,
        repo=repo_name,
        file=args.file or "",
        line=args.line,
        type=args.type or "bug",
        severity=args.finding_severity or "high",
        pattern=args.pattern or "",
        title=args.title or "",
        description=args.description or "",
        status=args.status or "found",
        github_url=args.url or "",
        found_by=args.found_by or "",
        verified=args.verified or False,
    )

    try:
        result_id = add_finding(base_path, finding)
        print(f"Added: {result_id}")
        save_index(base_path)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _findings_list(base_path: str, args) -> None:
    findings = list_findings(
        base_path,
        repo_name=args.repo_name,
        status_filter=args.status,
        type_filter=args.type,
    )

    if not findings:
        print("No findings found.")
        return

    if args.format == "json":
        print(json.dumps(findings, indent=2, ensure_ascii=False))
        return

    # Markdown table
    print(f"{len(findings)} finding(s):\n")
    for f in findings:
        sev_icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(f.get("severity", ""), "?")
        status = f.get("status", "?")
        url = f.get("github_url", "")
        title = f.get("title", "(no title)")
        repo = f.get("repo", "?")
        file_ = f.get("file", "?")
        line = f.get("line")
        loc = f"{file_}:{line}" if line else file_

        status_display = status
        if url:
            status_display = f"{status} ({url})"

        print(f"  {sev_icon} [{f.get('id', '?')}] {title}")
        print(f"    {repo} | {loc} | {f.get('type', '?')} | {status_display}")
        if f.get("pattern"):
            print(f"    pattern: {f['pattern']}")
        print()


def _findings_update(base_path: str, args) -> None:
    try:
        update_status(
            base_path,
            repo_name=args.repo_name or "",  # empty string → search all files
            finding_id=args.finding_id,
            new_status=args.new_status,
            github_url=args.url or "",
        )
        print(f"Updated: {args.finding_id} → {args.new_status}")
        save_index(base_path)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _findings_search(base_path: str, args) -> None:
    results = search_findings(base_path, args.query)
    if not results:
        print(f"No findings matching '{args.query}'.")
        return
    print(f"{len(results)} result(s) for '{args.query}':\n")
    for f in results:
        print(f"  [{f.get('id', '?')}] {f.get('title', '?')} ({f.get('repo', '?')})")


def _findings_stats(base_path: str, args) -> None:
    stats = get_stats(base_path, repo_name=args.repo_name)

    if args.format == "json":
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    print(f"Total: {stats['total']} findings\n")

    print("By status:")
    for k, v in sorted(stats["by_status"].items()):
        print(f"  {k}: {v}")

    print("\nBy repository:")
    for k, v in sorted(stats["by_repo"].items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    print("\nBy severity:")
    for k, v in sorted(stats["by_severity"].items()):
        print(f"  {k}: {v}")

    print("\nBy type:")
    for k, v in sorted(stats["by_type"].items()):
        print(f"  {k}: {v}")


def _findings_index(base_path: str, args) -> None:
    path = save_index(base_path)
    print(f"Index generated: {path}")
