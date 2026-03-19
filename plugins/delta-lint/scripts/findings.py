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
SCAN_HISTORY_FILE = ".delta-lint/scan_history.jsonl"
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
    "false_positive",  # 偽陽性（コード確認で否定）
)


# Category is a free-form string — not validated.
# Known values (extensible):
#   "contradiction"  — two modules contradict each other (①-⑥)
#   "structural"     — not broken yet, but fragile (⑦-⑩)
#   "deep:hook"      — deep scan: hook-related mismatch
#   "deep:constant"  — deep scan: constant conflict
#   "deep:class"     — deep scan: missing parent class
# Add new categories freely; no whitelist enforcement.

# --- Debt Score Calculation ---
# Weights are centralized in scoring.py. Module-level references for
# backward compatibility and use in contexts without repo_path.
from scoring import (
    DEFAULT_STATUS_MULTIPLIER as STATUS_MULTIPLIER,
    ScoringConfig,
    load_scoring_config,
    compute_roi,
)


def finding_debt_score(f: dict, cfg: ScoringConfig | None = None) -> float:
    """Calculate debt coefficient for a single finding (0〜1.0).

    debt_coefficient = severity × pattern × status
    merged/wontfix → 0 (resolved), but history remains in JSONL.

    If cfg is provided, uses team-customized weights from config.json.
    Otherwise uses built-in defaults.
    """
    from scoring import debt_coefficient as _dc
    return _dc(
        f.get("severity", "low"),
        f.get("pattern", ""),
        f.get("status", "found"),
        cfg,
    )


def compute_debt_summary(findings: list[dict], cfg: ScoringConfig | None = None) -> dict:
    """Compute aggregate debt metrics from a list of findings.

    Returns dict with: total_debt, active_debt, active_count, total_count, resolution_rate.
    Scores are on 0〜1000 per-finding scale, so total can be thousands for large codebases.
    """
    sm = cfg.status_multiplier if cfg else STATUS_MULTIPLIER
    active = [f for f in findings if sm.get(f.get("status"), 1.0) > 0]
    total_debt = sum(finding_debt_score(f, cfg) for f in findings)
    active_debt = sum(finding_debt_score(f, cfg) for f in active)
    return {
        "total_debt": round(total_debt, 1),
        "active_debt": round(active_debt, 1),
        "active_count": len(active),
        "total_count": len(findings),
        "resolution_rate": round((1 - len(active) / max(len(findings), 1)) * 100),
    }


# ---------------------------------------------------------------------------
# Scan history tracking
# ---------------------------------------------------------------------------

def append_scan_history(
    base_path: str | Path,
    *,
    clusters: int = 0,
    findings_count: int = 0,
    duration_sec: float = 0.0,
    scan_type: str = "existing",  # "existing" | "diff" | "stress" | "deep"
    finding_ids: list[str] | None = None,
    patterns_found: list[str] | None = None,
    scope: str = "",    # 3-axis: "diff" | "smart" | "all"
    depth: str = "",    # 3-axis: "1hop" | "graph"
    lens: str = "",     # 3-axis: "default" | "stress" | "security"
) -> None:
    """Append a scan record to scan_history.jsonl.

    finding_ids: Chao1 推定に使用。このスキャンで検出された finding ID 一覧。
    patterns_found: パターン別 surprise 計算に使用。検出されたパターン番号一覧。
    scope/depth/lens: 3-axis scan model。coverage matrix で使用。
    """
    path = Path(base_path) / SCAN_HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now().isoformat(),
        "scan_type": scan_type,
        "clusters": clusters,
        "findings_count": findings_count,
        "duration_sec": round(duration_sec, 1),
    }
    # 3-axis fields (infer from scan_type if not explicitly provided)
    if scope:
        record["scope"] = scope
    elif scan_type == "diff":
        record["scope"] = "diff"
    elif scan_type in ("existing", "deep"):
        record["scope"] = "smart"
    elif scan_type == "stress":
        record["scope"] = "all"
    if depth:
        record["depth"] = depth
    elif scan_type == "deep":
        record["depth"] = "graph"
    else:
        record["depth"] = "1hop"
    if lens:
        record["lens"] = lens
    elif scan_type == "stress":
        record["lens"] = "stress"
    else:
        record["lens"] = "default"
    if finding_ids is not None:
        record["finding_ids"] = finding_ids
    if patterns_found is not None:
        record["patterns_found"] = sorted(set(patterns_found))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_scan_history(base_path: str | Path) -> list[dict]:
    """Load all scan history records."""
    path = Path(base_path) / SCAN_HISTORY_FILE
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def compute_scan_depth(base_path: str | Path) -> dict:
    """Compute scan depth/confidence from history.

    Returns dict with: scan_count, total_clusters, grade, last_scan.
    Grade: D(初回) → C(2-3回) → B(4-6回) → A(7+回)
    """
    history = load_scan_history(base_path)
    scan_count = len(history)
    total_clusters = sum(r.get("clusters", 0) for r in history)
    last_scan = history[-1].get("timestamp", "")[:16].replace("T", " ") if history else ""

    if scan_count == 0:
        grade = "-"
    elif scan_count == 1:
        grade = "D"
    elif scan_count <= 3:
        grade = "C"
    elif scan_count <= 6:
        grade = "B"
    else:
        grade = "A"

    return {
        "scan_count": scan_count,
        "total_clusters": total_clusters,
        "grade": grade,
        "last_scan": last_scan,
    }


# ---------------------------------------------------------------------------
# Coverage matrix (scope × depth × lens)
# ---------------------------------------------------------------------------

# Map legacy scan_type to 3-axis model
_SCAN_TYPE_TO_AXES = {
    "diff":     {"scope": "diff",  "depth": "1hop",  "lens": "default"},
    "existing": {"scope": "smart", "depth": "1hop",  "lens": "default"},
    "deep":     {"scope": "all",   "depth": "graph", "lens": "default"},
    "stress":   {"scope": "all",   "depth": "1hop",  "lens": "stress"},
}

# All valid axis values
_SCOPES = ["diff", "smart", "all"]
_DEPTHS = ["1hop", "graph"]
_LENSES = ["default", "stress", "security"]

# Human-readable labels
_SCOPE_LABELS = {"diff": "変更差分", "smart": "履歴優先", "all": "全ファイル"}
_DEPTH_LABELS = {"1hop": "1-hop依存", "graph": "構造グラフ"}
_LENS_LABELS = {"default": "構造矛盾検査", "stress": "ストレステスト", "security": "セキュリティ"}

# Command fragments per axis value
_SCOPE_FLAGS = {"diff": "", "smart": "--scope smart", "all": "--scope all"}
_DEPTH_FLAGS = {"1hop": "", "graph": "--depth graph"}
_LENS_FLAGS = {"default": "", "stress": "--lens stress", "security": "--lens security"}


def compute_coverage_matrix(base_path: str | Path) -> dict:
    """Compute a 3-axis coverage matrix from scan history.

    Unified matrix: rows = scope × depth (6 rows), cols = lens (3 cols) = 18 cells.

    Returns:
        {
            "cells": [
                {
                    "scope": "diff", "depth": "1hop", "lens": "default",
                    "scope_label": "変更差分", "depth_label": "1-hop依存", "lens_label": "矛盾+負債",
                    "count": 3, "last_run": "2026-03-19 14:30",
                    "command": "delta scan",
                    "is_flow": true,
                },
                ...
            ],
            "cells_done": 3,     # 実行済みセル数
            "cells_total": 18,   # 全セル数
        }
    """
    history = load_scan_history(Path(base_path))

    # Count per (scope, depth, lens) combination
    counts: dict[tuple, list] = {}
    for record in history:
        scan_type = record.get("scan_type", "diff")
        # Support new-style records with explicit axes
        if "scope" in record:
            axes = {
                "scope": record["scope"],
                "depth": record.get("depth", "1hop"),
                "lens": record.get("lens", "default"),
            }
        else:
            axes = _SCAN_TYPE_TO_AXES.get(scan_type, _SCAN_TYPE_TO_AXES["diff"])
        key = (axes["scope"], axes["depth"], axes["lens"])
        counts.setdefault(key, []).append(record.get("timestamp", ""))

    cells = []
    cells_done = 0

    for scope in _SCOPES:
        for depth in _DEPTHS:
            for lens in _LENSES:
                key = (scope, depth, lens)
                records = counts.get(key, [])
                count = len(records)
                last_run = ""
                if records:
                    last_ts = sorted(records)[-1]
                    last_run = last_ts[:16].replace("T", " ") if last_ts else ""

                # Build command
                flags = [f for f in [
                    _SCOPE_FLAGS.get(scope, ""),
                    _DEPTH_FLAGS.get(depth, ""),
                    _LENS_FLAGS.get(lens, ""),
                ] if f]
                command = "delta scan" + (" " + " ".join(flags) if flags else "")

                cell = {
                    "scope": scope,
                    "depth": depth,
                    "lens": lens,
                    "scope_label": _SCOPE_LABELS[scope],
                    "depth_label": _DEPTH_LABELS[depth],
                    "lens_label": _LENS_LABELS[lens],
                    "count": count,
                    "last_run": last_run,
                    "command": command,
                    "is_flow": (scope == "diff"),
                }
                cells.append(cell)

                if count > 0:
                    cells_done += 1

    cells_total = len(_SCOPES) * len(_DEPTHS) * len(_LENSES)  # 18

    return {
        "cells": cells,
        "cells_done": cells_done,
        "cells_total": cells_total,
    }


def apply_policy(findings: list[dict], policy: dict) -> list[dict]:
    """Apply team policy to findings (post-detection).

    - accepted: remove findings matching id or pattern+file glob
    - severity_overrides: adjust severity for matching findings

    Returns filtered list (accepted findings are removed).
    """
    if not policy:
        return findings

    accepted_rules = policy.get("accepted", [])
    severity_rules = policy.get("severity_overrides", [])

    if not accepted_rules and not severity_rules:
        return findings

    # Build accepted lookup
    accepted_ids: set[str] = set()
    accepted_patterns: list[dict] = []
    for rule in accepted_rules:
        if "id" in rule:
            accepted_ids.add(rule["id"])
        elif "pattern" in rule:
            accepted_patterns.append(rule)

    result = []
    for f in findings:
        # Check accepted by ID
        fid = f.get("id", "")
        if fid and fid in accepted_ids:
            continue

        # Check accepted by pattern + file glob
        f_pattern = f.get("pattern", "")
        f_file = _finding_file(f)
        is_accepted = False
        for rule in accepted_patterns:
            if rule.get("pattern") and rule["pattern"] != f_pattern:
                continue
            rule_file = rule.get("file", "")
            if rule_file and not _glob_match(f_file, rule_file):
                continue
            is_accepted = True
            break

        if is_accepted:
            continue

        # Apply severity overrides
        for rule in severity_rules:
            rule_pattern = rule.get("pattern", "")
            rule_file = rule.get("file", "")
            new_sev = rule.get("severity", "")
            if rule_pattern and rule_pattern != f_pattern:
                continue
            if rule_file and not _glob_match(f_file, rule_file):
                continue
            if new_sev in ("high", "medium", "low"):
                f = dict(f)  # copy to avoid mutating original
                f["severity"] = new_sev
                break

        result.append(f)

    return result


def _finding_file(f: dict) -> str:
    """Extract file path from a finding dict."""
    # findings from detect() have location.file_a
    loc = f.get("location", {})
    if isinstance(loc, dict):
        return loc.get("file_a", f.get("file", ""))
    return f.get("file", "")


def _glob_match(filepath: str, pattern: str) -> bool:
    """Simple glob match: supports * wildcard at end of path segments.

    Examples:
      _glob_match("src/legacy/old.ts", "src/legacy/*") → True
      _glob_match("src/api/v2/handler.ts", "src/api/*") → True
      _glob_match("src/core/main.ts", "src/api/*") → False
      _glob_match("src/auth.ts", "src/auth.ts") → True (exact)
    """
    if not pattern:
        return True
    if pattern == filepath:
        return True
    # Handle trailing wildcard: "src/legacy/*" matches anything under src/legacy/
    if pattern.endswith("/*"):
        prefix = pattern[:-1]  # "src/legacy/"
        return filepath.startswith(prefix)
    # Handle single * in middle (fnmatch-style)
    import fnmatch
    return fnmatch.fnmatch(filepath, pattern)


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
    category: str = ""  # legacy — use taxonomies instead
    taxonomies: dict | None = None
    # WordPress-style taxonomy/term system. Values can be str or list[str].
    # Example: {"category": "deep:hook", "certainty": "definite",
    #           "assignee": ["tanaka", "suzuki"], "milestone": "v2.1"}
    churn_6m: int = 0       # git commits touching file in last 6 months
    fan_out: int = 0        # number of files referencing this file
    total_lines: int = 0    # line count of primary file (for entropy)


def _findings_dir(base_path: str | Path) -> Path:
    return Path(base_path) / FINDINGS_DIR


def _repo_file(base_path: str | Path, repo_name: str) -> Path:
    """Get JSONL file path for a repo. Sanitize name for filesystem."""
    safe_name = repo_name.replace("/", "__").replace("\\", "__")
    return _findings_dir(base_path) / f"{safe_name}.jsonl"


def generate_id(repo: str, file: str, title: str,
                file_b: str = "", pattern: str = "") -> str:
    """Generate a short deterministic ID.

    When file_b and pattern are provided, uses structural identity
    (file pair + pattern) instead of title — immune to LLM wording variance.
    Falls back to repo:file:title for manual additions or single-file findings.
    """
    if file_b and pattern:
        files = sorted([file, file_b])
        key = f"{repo}:{files[0]}:{files[1]}:{pattern}"
    else:
        key = f"{repo}:{file}:{title}"
    h = hashlib.sha256(key.encode()).hexdigest()[:8]
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


def _migrate_taxonomies(entry: dict) -> dict:
    """Migrate legacy category field into taxonomies dict."""
    if entry.get("taxonomies") is None:
        entry["taxonomies"] = {}
    # Legacy category → taxonomies["category"]
    cat = entry.get("category", "")
    if cat and "category" not in entry["taxonomies"]:
        entry["taxonomies"]["category"] = cat
    return entry


def _get_latest(lines: list[dict]) -> dict[str, dict]:
    """Collapse event log: for each id, keep the latest entry."""
    latest: dict[str, dict] = {}
    for entry in lines:
        fid = entry.get("id", "")
        if fid:
            latest[fid] = _migrate_taxonomies(entry)
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
    elif args.findings_command == "dashboard":
        _findings_dashboard(base_path, args)
    elif args.findings_command == "enrich":
        _findings_enrich(base_path, args)
    elif args.findings_command == "verify-top":
        _findings_verify_top(base_path, args)
    else:
        print("Usage: delta-lint findings {add|list|update|search|stats|index|dashboard|enrich|verify-top}", file=sys.stderr)
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


def _findings_dashboard(base_path: str, args) -> None:
    # Build treemap JSON if stress-test results exist
    treemap_json = None
    results_path = Path(base_path) / ".delta-lint" / "stress-test" / "results.json"
    if results_path.exists():
        try:
            from visualize import build_treemap_json
            treemap_json = build_treemap_json(str(results_path))
        except Exception:
            pass
    path = generate_dashboard(base_path, treemap_json=treemap_json)
    print(f"Dashboard generated: {path}")


def _findings_enrich(base_path: str, args) -> None:
    """Enrich findings with git churn, fan-out, and line count data."""
    from git_enrichment import enrich_findings_batch

    findings = list_findings(base_path)
    if not findings:
        print("No findings to enrich.", file=sys.stderr)
        return

    before = sum(1 for f in findings if f.get("churn_6m") or f.get("fan_out"))
    enrich_findings_batch(findings, base_path, verbose=True)
    after = sum(1 for f in findings if f.get("churn_6m") or f.get("fan_out"))

    # Write back to all JSONL files
    findings_dir = Path(base_path) / ".delta-lint" / "findings"
    enriched_map = {f["id"]: f for f in findings}
    updated_total = 0

    for jsonl_path in findings_dir.glob("*.jsonl"):
        with open(jsonl_path, "r") as fh:
            lines = [l.strip() for l in fh.readlines() if l.strip()]
        new_lines = []
        updated = 0
        for line in lines:
            obj = json.loads(line)
            fid = obj.get("id")
            if fid in enriched_map:
                enriched = enriched_map[fid]
                for key in ("churn_6m", "fix_churn_6m", "fan_out", "total_lines"):
                    if enriched.get(key) and not obj.get(key):
                        obj[key] = enriched[key]
                        updated += 1
            new_lines.append(json.dumps(obj, ensure_ascii=False))
        with open(jsonl_path, "w") as fh:
            fh.write("\n".join(new_lines) + "\n")
        updated_total += updated

    print(f"Enriched: {before} → {after} findings with git data ({updated_total} fields written)")


def _findings_verify_top(base_path: str, args) -> None:
    """Re-verify top 1/3 of findings by priority score.

    Reads source files referenced by each finding, sends to verifier LLM,
    and updates status to 'verified' (confirmed) or 'wontfix' (rejected).
    """
    from scoring import compute_roi
    from info_theory import finding_information_score

    findings = list_findings(base_path)
    if not findings:
        print("No findings to verify.", file=sys.stderr)
        return

    # Filter to actionable statuses only
    actionable_statuses = {"found", "verified"}
    candidates = [f for f in findings if f.get("status", "found") in actionable_statuses]
    if not candidates:
        print("No actionable findings (all already resolved).", file=sys.stderr)
        return

    # Score and sort by priority
    scan_history = load_scan_history(base_path)
    for f in candidates:
        try:
            info = finding_information_score(f, scan_history).get("info_score", 0)
        except Exception:
            info = 0
        try:
            roi = compute_roi(
                severity=f.get("severity", "low"),
                churn_6m=f.get("churn_6m", 0),
                fan_out=f.get("fan_out", 0),
                pattern=f.get("pattern", ""),
                fix_churn_6m=f.get("fix_churn_6m"),
                user_facing=bool(f.get("user_facing")),
                found_at=f.get("found_at", ""),
                status=f.get("status", "found"),
            ).get("roi_score", 0)
        except Exception:
            roi = 0
        sev_bonus = {"high": 300, "medium": 100, "low": 0}.get(f.get("severity", "low"), 0)
        f["_priority"] = info + roi + sev_bonus

    candidates.sort(key=lambda x: -x.get("_priority", 0))

    # Top 1/3 (minimum 3, maximum all)
    n = max(3, len(candidates) // 3)
    targets = candidates[:n]

    print(f"Verifying top {len(targets)}/{len(candidates)} findings by priority...",
          file=sys.stderr)

    # Build lightweight context: read source files for each finding
    source_files: dict[str, str] = {}
    for f in targets:
        loc = f.get("location", {})
        file_a = loc.get("file_a", f.get("file", ""))
        file_b = loc.get("file_b", "")
        for fpath in [file_a, file_b]:
            if not fpath or fpath in source_files:
                continue
            full = Path(base_path) / fpath
            if full.exists():
                try:
                    source_files[fpath] = full.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    # Create a minimal context object compatible with verifier
    class _FindingsContext:
        """Lightweight context for re-verification of stored findings."""
        def to_prompt_string(self) -> str:
            parts = []
            for fpath, content in source_files.items():
                parts.append(f"### {fpath}\n```\n{content}\n```")
            return "\n\n".join(parts) if parts else "(source files not available)"

    context = _FindingsContext()

    # Run verifier
    from verifier import verify_findings
    model = getattr(args, "model", "claude-sonnet-4-20250514")
    backend = getattr(args, "backend", "cli")

    confirmed, rejected, meta = verify_findings(
        targets, context,
        model=model, backend=backend,
        confidence_threshold=0.7,
        verbose=True,
    )

    # Update statuses in JSONL
    confirmed_ids = {f.get("id") for f in confirmed}
    rejected_ids = {f.get("id") for f in rejected}
    reject_reasons = {f.get("id"): f.get("_verify_reason", "") for f in rejected}

    findings_dir = Path(base_path) / ".delta-lint" / "findings"
    status_updates = 0

    for jsonl_path in findings_dir.glob("*.jsonl"):
        with open(jsonl_path, "r") as fh:
            lines = [l.strip() for l in fh.readlines() if l.strip()]
        new_lines = []
        for line in lines:
            obj = json.loads(line)
            fid = obj.get("id")
            if fid in confirmed_ids and obj.get("status") == "found":
                obj["status"] = "verified"
                obj["verified"] = True
                status_updates += 1
            elif fid in rejected_ids:
                obj["status"] = "wontfix"
                obj["_verify_reason"] = reject_reasons.get(fid, "")
                status_updates += 1
            new_lines.append(json.dumps(obj, ensure_ascii=False))
        with open(jsonl_path, "w") as fh:
            fh.write("\n".join(new_lines) + "\n")

    # Summary
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"Verify-top summary:", file=sys.stderr)
    print(f"  Verified (confirmed): {len(confirmed)}", file=sys.stderr)
    print(f"  Rejected (wontfix):   {len(rejected)}", file=sys.stderr)
    print(f"  Status updates:       {status_updates}", file=sys.stderr)
    if rejected:
        print(f"\n  Rejected findings:", file=sys.stderr)
        for f in rejected:
            fid = f.get("id", "?")
            reason = f.get("_verify_reason", "no reason")
            print(f"    ✗ {fid}: {reason}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Stress-test → debt findings conversion
# ---------------------------------------------------------------------------

def ingest_stress_test_debt(base_path: str | Path) -> list[str]:
    """Convert high-risk files from stress-test results into debt findings.

    Reads results.json + structure.json, calculates per-file risk scores,
    and registers files above threshold as debt findings (⑧ or ⑩).

    Returns list of finding IDs added (skips duplicates).
    """
    base = Path(base_path)
    results_path = base / ".delta-lint" / "stress-test" / "results.json"
    structure_path = base / ".delta-lint" / "stress-test" / "structure.json"

    if not results_path.exists():
        return []

    data = json.loads(results_path.read_text(encoding="utf-8"))
    repo_name = data.get("metadata", {}).get("repo_name", base.name)

    # Build per-file risk: sum severity scores across all modifications
    file_risk: dict[str, int] = {}
    file_findings_count: dict[str, int] = {}
    for r in data.get("results", []):
        mod = r.get("modification", {})
        target = mod.get("file", "")
        findings = r.get("findings", [])
        for f in findings:
            score = {"high": 3, "medium": 2, "low": 1}.get(f.get("severity", "low"), 1)
            for af in mod.get("affected_files", [target]):
                file_risk[af] = file_risk.get(af, 0) + score
                file_findings_count[af] = file_findings_count.get(af, 0) + 1

    if not file_risk:
        return []

    # Load structure.json for dependency/role info
    modules_by_path: dict[str, dict] = {}
    hotspot_paths: set[str] = set()
    if structure_path.exists():
        try:
            struct = json.loads(structure_path.read_text(encoding="utf-8"))
            for m in struct.get("modules", []):
                modules_by_path[m["path"]] = m
            for h in struct.get("hotspots", []):
                hotspot_paths.add(h.get("path", ""))
        except (json.JSONDecodeError, KeyError):
            pass

    # Threshold: top 30% of risk scores, minimum score of 10
    scores = sorted(file_risk.values(), reverse=True)
    threshold = max(scores[max(len(scores) // 3, 1) - 1] if scores else 10, 10)

    added: list[str] = []
    for filepath, risk_score in sorted(file_risk.items(), key=lambda x: -x[1]):
        if risk_score < threshold:
            continue

        mod_info = modules_by_path.get(filepath, {})
        deps = mod_info.get("dependencies", [])
        fan_out = len(deps)
        is_hotspot = filepath in hotspot_paths
        n_findings = file_findings_count.get(filepath, 0)

        # Choose pattern: ⑩ if high fan-out (hub), ⑧ if findings suggest drift
        pattern = "⑩" if fan_out >= 3 or is_hotspot else "⑧"

        # Severity: high risk → medium, moderate → low
        severity = "medium" if risk_score >= threshold * 2 else "low"

        title = f"構造的脆弱性: {filepath}"
        fid = generate_id(repo_name, filepath, title)

        description_parts = [
            f"ストレステストで {n_findings}件の仮想改修が影響。リスクスコア {risk_score}。",
        ]
        if fan_out > 0:
            description_parts.append(f"依存先 {fan_out} モジュール。")
        if is_hotspot:
            description_parts.append("構造解析でホットスポット判定。")
        if mod_info.get("role"):
            description_parts.append(f"役割: {mod_info['role']}")

        finding = Finding(
            id=fid,
            repo=repo_name,
            file=filepath,
            type="contradiction",
            severity=severity,
            pattern=pattern,
            title=title,
            description=" ".join(description_parts),
            status="found",
            found_by="stress-test",
            category="debt",
            tags=["stress-test", "structural-fragility"],
            taxonomies={"certainty": "uncertain", "category": "debt"},
        )

        try:
            add_finding(base_path, finding)
            added.append(fid)
        except ValueError:
            pass  # duplicate — already registered

    return added


# ---------------------------------------------------------------------------
# ROI data loaders (churn / fan_out from existing delta-lint artifacts)
# ---------------------------------------------------------------------------

def _load_churn_map(base_path: str | Path) -> dict[str, int]:
    """Load file → change_count map from git history.

    Sources (in priority order):
    1. .delta-lint/stress-test/structure.json → dev_patterns (has churn evidence)
    2. Live git log (if repo is a git repo)

    Returns {relative_path: change_count_in_6_months}.
    """
    base = Path(base_path)
    churn: dict[str, int] = {}

    # Try live git log first (most accurate)
    try:
        import subprocess
        result = subprocess.run(
            ["git", "log", "--since=6 months ago", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, cwd=str(base), timeout=15,
        )
        if result.returncode == 0:
            from collections import Counter
            files = [
                line.strip() for line in result.stdout.splitlines()
                if line.strip() and not line.startswith(" ")
            ]
            churn = dict(Counter(files))
    except Exception:
        pass

    return churn


def _load_fan_out_map(base_path: str | Path) -> dict[str, int]:
    """Load file → fan_out (被参照数) map.

    Uses grep on require/include/import statements for accurate reference counting.
    Falls back to structure.json dependencies if grep fails.
    """
    import subprocess
    from collections import Counter

    base = Path(base_path)
    fan_out: dict[str, int] = {}

    # PHP: grep require/require_once/include/include_once statements
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.php",
             "-E", r"(require|include)(_once)?\s*[\(\s]"],
            capture_output=True, text=True, cwd=str(base), timeout=15,
        )
        if result.returncode <= 1:  # 0=found, 1=not found
            dep_counts: Counter = Counter()
            for line in result.stdout.splitlines():
                # 参照されているファイル名を抽出
                import re
                # require_once( __DIR__ . '/foo.php' ) やシンプルな形式に対応
                m = re.search(r"['\"]([^'\"]+\.php)['\"]", line)
                if not m:
                    continue
                ref_name = m.group(1)
                # パス末尾のファイル名だけ取得
                ref_basename = ref_name.split("/")[-1]
                dep_counts[ref_basename] += 1
            # basename → full relative path のマッピングを構築
            all_php = subprocess.run(
                ["find", ".", "-name", "*.php", "-not", "-path", "*/vendor/*",
                 "-not", "-path", "*/.delta-lint/*"],
                capture_output=True, text=True, cwd=str(base), timeout=10,
            )
            basename_to_paths: dict[str, list[str]] = {}
            for p in all_php.stdout.splitlines():
                p = p.strip().lstrip("./")
                if p:
                    bn = p.split("/")[-1]
                    basename_to_paths.setdefault(bn, []).append(p)
            # basename のカウントを full path に展開
            for bn, count in dep_counts.items():
                for full_path in basename_to_paths.get(bn, []):
                    fan_out[full_path] = max(fan_out.get(full_path, 0), count)
    except Exception:
        pass

    # TS/JS: grep import statements
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.ts", "--include=*.js",
             "-E", r"(import\s|from\s['\"])"],
            capture_output=True, text=True, cwd=str(base), timeout=15,
        )
        if result.returncode <= 1:
            import re
            dep_counts_ts: Counter = Counter()
            for line in result.stdout.splitlines():
                m = re.search(r"from\s+['\"]([^'\"]+)['\"]", line)
                if not m:
                    m = re.search(r"import\s+['\"]([^'\"]+)['\"]", line)
                if not m:
                    continue
                ref = m.group(1).split("/")[-1]
                # 拡張子を正規化
                for ext in [".ts", ".js", ""]:
                    dep_counts_ts[ref + ext] += 1
            all_ts = subprocess.run(
                ["find", ".", "-name", "*.ts", "-o", "-name", "*.js"],
                capture_output=True, text=True, cwd=str(base), timeout=10,
            )
            bn_to_paths: dict[str, list[str]] = {}
            for p in all_ts.stdout.splitlines():
                p = p.strip().lstrip("./")
                if p:
                    bn = p.split("/")[-1]
                    bn_to_paths.setdefault(bn, []).append(p)
            for bn, count in dep_counts_ts.items():
                for full_path in bn_to_paths.get(bn, []):
                    fan_out[full_path] = max(fan_out.get(full_path, 0), count)
    except Exception:
        pass

    # Fallback: structure.json の dependencies も統合（grep で取れなかったもの）
    structure_path = base / ".delta-lint" / "stress-test" / "structure.json"
    if structure_path.exists():
        try:
            data = json.loads(structure_path.read_text(encoding="utf-8"))
            modules = data.get("modules", [])
            struct_counts: Counter = Counter()
            for mod in modules:
                for dep in mod.get("dependencies", []):
                    struct_counts[dep] += 1
            for path, count in struct_counts.items():
                if path not in fan_out:
                    fan_out[path] = count
        except Exception:
            pass

    return fan_out


# ---------------------------------------------------------------------------
# Dashboard generation
# ---------------------------------------------------------------------------

def generate_dashboard(
    base_path: str | Path,
    *,
    scan_progress: dict | None = None,
    treemap_json: str | None = None,
    dashboard_template: str = "",
) -> Path:
    """Generate a self-contained HTML dashboard from all findings.

    scan_progress: optional dict with keys:
        completed (int), total (int), is_complete (bool)
    When provided and not is_complete, the dashboard includes a progress bar
    and auto-refresh meta tag so the browser shows live updates.

    dashboard_template: override template path. Resolution order:
        1. This parameter (profile policy.dashboard_template)
        2. .delta-lint/templates/findings_dashboard.html (repo-local)
        3. Built-in scripts/templates/findings_dashboard.html
    """
    from string import Template as StrTemplate

    base_path = Path(base_path)
    findings = list_findings(base_path)
    stats = get_stats(base_path)

    # Resolve template: explicit override > repo-local > built-in
    if dashboard_template:
        tp = Path(dashboard_template)
        if not tp.is_absolute():
            tp = base_path / tp
        template_path = tp if tp.exists() else Path(__file__).parent / "templates" / "findings_dashboard.html"
    else:
        repo_local = base_path / ".delta-lint" / "templates" / "findings_dashboard.html"
        if repo_local.exists():
            template_path = repo_local
        else:
            template_path = Path(__file__).parent / "templates" / "findings_dashboard.html"
    template = StrTemplate(template_path.read_text(encoding="utf-8"))

    sev_counts = stats.get("by_severity", {})
    status_counts = stats.get("by_status", {})

    # KPI: 検証済み = 人間が status を verified に変更したもののみ
    # 未検証 = found のまま残っている findings
    resolved_statuses = {"merged", "wontfix", "duplicate", "rejected", "false_positive"}
    confirmed_bugs = sum(
        1 for f in findings
        if f.get("status") == "verified"
    )
    investigating = sum(
        1 for f in findings
        if f.get("status", "found") == "found"
    )
    # 技術的負債合計（active findings のみ）— per-finding 計算後に集計するため後で算出

    # Scan depth
    scan_depth = compute_scan_depth(base_path)

    scoring_cfg = load_scoring_config(base_path)

    # --- ROI data: churn, fan_out, roi_score ---
    # Priority: JSONL stored values > live git > 0
    churn_map = _load_churn_map(base_path)
    fan_out_map = _load_fan_out_map(base_path)

    # Count findings per file to distribute fan_out fairly
    from collections import Counter as _Counter
    _file_finding_count = _Counter(_finding_file(f) for f in findings)

    for f in findings:
        file_a = _finding_file(f)
        churn_val = f.get("churn_6m") or churn_map.get(file_a, 0)
        # Always use file-level fan_out from live map (JSONL stores pre-distributed values)
        file_fan_out = fan_out_map.get(file_a, 0) or f.get("fan_out", 0)
        # Distribute file-level fan_out across findings in the same file
        n_in_file = _file_finding_count.get(file_a, 1)
        effective_fan_out = max(round(file_fan_out / n_in_file), 1) if file_fan_out > 0 else 0
        roi = compute_roi(
            severity=f.get("severity", "low"),
            churn_6m=churn_val,
            fan_out=effective_fan_out,
            pattern=f.get("pattern", ""),
            cfg=scoring_cfg,
            fix_churn_6m=f.get("fix_churn_6m"),
            user_facing=bool(f.get("user_facing")),
            found_at=f.get("found_at", ""),
            status=f.get("status", "found"),
        )
        f["churn"] = roi["churn_6m"]
        f["churn_6m"] = churn_val  # preserve for info_theory
        f["fan_out"] = roi["fan_out"]
        f["fan_out_file"] = file_fan_out  # original file-level fan_out
        f["total_lines"] = f.get("total_lines", 0)
        f["user_facing_weight"] = roi["user_facing_weight"]
        f["age_multiplier"] = roi["age_multiplier"]
        f["debt_coefficient"] = roi["debt_coefficient"]
        f["context_score"] = roi["context_score"]
        # Discount scores for uncertain findings (e.g. structural fragility from stress-test)
        certainty = (f.get("taxonomies") or {}).get("certainty", "")
        is_uncertain = certainty == "uncertain" or "構造的脆弱性" in f.get("title", "")
        discount = 0.3 if is_uncertain else 1.0
        f["roi_score"] = round(roi["roi_score"] * discount, 1)
        f["debt_score"] = f["roi_score"]  # 技術的負債 = debt_coefficient × context_score

    # Merge suppress data (approved_by) into findings
    suppress_lookup: dict[str, dict] = {}
    try:
        from suppress import load_suppressions
        suppressions = load_suppressions(str(base_path))
        for s in suppressions:
            suppress_lookup[s.finding_hash] = {
                "approved_by": s.approved_by,
                "why": s.why,
                "why_type": s.why_type,
                "author": s.author,
            }
    except Exception:
        pass

    # Attach approval info to findings
    for f in findings:
        fid = f.get("id", "")
        if fid in suppress_lookup:
            f["approved_by"] = suppress_lookup[fid].get("approved_by", "")

    # 技術的負債合計（per-finding 計算後）
    active_findings = [f for f in findings if f.get("status", "found") not in resolved_statuses]
    debt_total = round(sum(f.get("debt_score", 0) for f in active_findings), 1)

    # Count planned debt (suppress + wontfix) and unapproved
    planned_debt = 0
    unapproved_count = 0
    for f in findings:
        status = f.get("status", "found")
        if status in ("wontfix", "duplicate"):
            planned_debt += 1
            if not f.get("approved_by"):
                unapproved_count += 1
    # Also count active suppressions
    planned_debt += len(suppress_lookup)
    for s_data in suppress_lookup.values():
        if not s_data.get("approved_by"):
            unapproved_count += 1

    debt = compute_debt_summary(findings, scoring_cfg)

    # --- Information-theoretic coverage estimation ---
    try:
        from info_theory import compute_coverage_from_history, finding_information_score
        scan_history = load_scan_history(base_path)
        coverage = compute_coverage_from_history(scan_history, findings)
        # Attach info_score to each finding
        for f in findings:
            info = finding_information_score(f, scan_history)
            certainty = (f.get("taxonomies") or {}).get("certainty", "")
            is_uncertain = certainty == "uncertain" or "構造的脆弱性" in f.get("title", "")
            discount = 0.3 if is_uncertain else 1.0
            f["info_score"] = round(info["info_score"] * discount, 1)
            f["surprise"] = info["surprise"]
    except Exception:
        coverage = {
            "estimated_total": len(findings), "coverage_pct": 100,
            "unseen_estimate": 0, "ci_lower": len(findings), "ci_upper": len(findings),
            "discovery_trend": "insufficient_data", "scans": 0,
        }

    # Build custom scoring badge for dashboard header
    from scoring import diff_from_defaults
    diffs = diff_from_defaults(scoring_cfg)
    if diffs:
        detail_lines = []
        for section, changes in diffs.items():
            for key, (default_val, custom_val) in changes.items():
                if default_val is not None:
                    detail_lines.append(f"{section}.{key}: {default_val} → {custom_val}")
                else:
                    detail_lines.append(f"{section}.{key}: {custom_val} (新規)")
        detail_text = "\n".join(detail_lines)
        custom_badge = (
            f'<span class="custom-badge" style="position:relative;">'
            f'カスタム設定あり'
            f'<span class="custom-detail">{detail_text}</span>'
            f'</span>'
        )
    else:
        custom_badge = ""

    # Build suppressions list for trade-off tab
    suppressions_data = []
    try:
        from suppress import load_suppressions
        all_suppressions = load_suppressions(str(base_path))
        for s in all_suppressions:
            suppressions_data.append({
                "id": s.id,
                "pattern": s.pattern,
                "files": s.files,
                "why": s.why,
                "why_type": s.why_type,
                "date": s.date,
                "author": s.author,
                "approved_by": s.approved_by,
            })
    except Exception:
        pass

    # Relative link to landmine map (from .delta-lint/findings/ to .delta-lint/stress-test/)
    landmine_map_path = base_path / ".delta-lint" / "stress-test" / "landmine_map.html"
    landmine_link = "../stress-test/landmine_map.html" if landmine_map_path.exists() else "#"

    # Build progress bar HTML + auto-refresh meta tag for streaming mode
    is_scanning = scan_progress and not scan_progress.get("is_complete", True)
    if is_scanning:
        sp_done = scan_progress.get("completed", 0)
        sp_total = scan_progress.get("total", 1)
        sp_pct = round(sp_done / max(sp_total, 1) * 100)
        progress_meta = '<meta http-equiv="refresh" content="3">'
        progress_html = (
            f'<div style="background:#0a7d5a;color:#fff;display:flex;align-items:center;gap:12px;'
            f'padding:8px 16px;font-size:13px;font-weight:500;shrink:0">'
            f'<span class="pulse-dot" style="display:inline-block;width:8px;height:8px;'
            f'border-radius:50%;background:#fff"></span>'
            f'スキャン中... {sp_done}/{sp_total} クラスタ完了 ({sp_pct}%)'
            f'<div style="flex:1;height:4px;background:rgba(255,255,255,.25);border-radius:2px;'
            f'min-width:80px;max-width:200px">'
            f'<div style="height:100%;width:{sp_pct}%;background:#fff;border-radius:2px;'
            f'transition:width .3s"></div></div></div>'
        )
    else:
        progress_meta = ""
        progress_html = ""

    html = template.safe_substitute(
        total_count=stats["total"],
        repo_count=len(stats.get("by_repo", {})),
        confirmed_bugs=confirmed_bugs,
        investigating=investigating,
        debt_total=debt_total,
        merged_count=status_counts.get("merged", 0),
        scan_count=scan_depth["scan_count"],
        scan_grade=scan_depth["grade"],
        scan_total_clusters=scan_depth["total_clusters"],
        scan_last=scan_depth["last_scan"],
        active_debt=debt["active_debt"],
        resolution_rate=debt["resolution_rate"],
        planned_debt=planned_debt,
        unapproved_count=unapproved_count,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        custom_scoring_badge=custom_badge,
        findings_json=json.dumps(findings, ensure_ascii=False),
        suppressions_json=json.dumps(suppressions_data, ensure_ascii=False),
        landmine_map_link=landmine_link,
        progress_meta=progress_meta,
        progress_html=progress_html,
        treemap_json=treemap_json if treemap_json else "null",
        coverage_pct=coverage["coverage_pct"],
        coverage_estimated=coverage["estimated_total"],
        coverage_unseen=coverage["unseen_estimate"],
        coverage_trend=coverage.get("discovery_trend", ""),
        coverage_ci_lower=coverage.get("ci_lower", 0),
        coverage_ci_upper=coverage.get("ci_upper", 0),
        coverage_json=json.dumps(coverage, ensure_ascii=False),
        coverage_matrix_json=json.dumps(compute_coverage_matrix(base_path), ensure_ascii=False),
    )

    out_path = _findings_dir(base_path) / "dashboard.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
