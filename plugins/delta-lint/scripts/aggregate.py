"""
Aggregation layer for delta-lint stress-test.

Collects per-modification scan results into per-file risk scores.
Standalone module — no LLM calls, no external dependencies beyond dataclasses.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from scoring import DEFAULT_SEVERITY_WEIGHT as SEVERITY_WEIGHT


@dataclass
class FileRisk:
    path: str
    hit_count: int = 0
    max_severity: str = "low"
    patterns: list[str] = field(default_factory=list)
    risk_score: float = 0.0
    findings: list[dict] = field(default_factory=list)
    confirmed_bugs: list[dict] = field(default_factory=list)
    lines: int = 0  # approximate file size for treemap tile sizing


def compute_risk_score(hit_count: int, n_modifications: int, max_severity: str) -> float:
    """risk_score = (hit_count / n_modifications) * severity_weight"""
    if n_modifications == 0:
        return 0.0
    weight = SEVERITY_WEIGHT.get(max_severity, 0.3)
    return min((hit_count / n_modifications) * weight, 1.0)


def _higher_severity(a: str, b: str) -> str:
    order = {"high": 0, "medium": 1, "low": 2}
    return a if order.get(a, 9) <= order.get(b, 9) else b


def aggregate_results(
    results: list[dict],
    n_modifications: int,
    confirmed_bugs: dict[str, list[dict]] | None = None,
) -> dict[str, FileRisk]:
    """Aggregate stress-test results into per-file risk scores.

    Args:
        results: List of modification result dicts from stress_test.py.
                 Each has: modification (dict), findings (list[dict])
        n_modifications: Total number of virtual modifications run
        confirmed_bugs: Optional map of file_path -> list of {issue, repo}

    Returns:
        Dict of file_path -> FileRisk
    """
    confirmed_bugs = confirmed_bugs or {}
    risks: dict[str, FileRisk] = {}

    for result in results:
        mod = result.get("modification", {})
        findings = result.get("findings", [])

        if not findings:
            continue

        # Collect all files involved in this modification's findings
        hit_files: set[str] = set()

        # The modification's target file
        target_file = mod.get("file", "")
        if target_file:
            hit_files.add(target_file)

        # Files mentioned in affected_files
        for af in mod.get("affected_files", []):
            hit_files.add(af)

        # Files mentioned in individual findings
        for finding in findings:
            for loc in finding.get("locations", []):
                f = loc.get("file", "")
                if f:
                    hit_files.add(f)

        # Update risk for each hit file
        for fpath in hit_files:
            if fpath not in risks:
                risks[fpath] = FileRisk(path=fpath)

            r = risks[fpath]
            r.hit_count += 1

            for finding in findings:
                severity = finding.get("severity", "low")
                r.max_severity = _higher_severity(r.max_severity, severity)

                pattern = finding.get("pattern", "")
                if pattern:
                    r.patterns.append(pattern)

                r.findings.append({
                    "modification_id": mod.get("id"),
                    "modification_desc": mod.get("description", ""),
                    **finding,
                })

    # Compute risk scores and attach confirmed bugs
    for fpath, r in risks.items():
        r.risk_score = compute_risk_score(r.hit_count, n_modifications, r.max_severity)
        if fpath in confirmed_bugs:
            r.confirmed_bugs = confirmed_bugs[fpath]

    return risks


def build_treemap_data(
    file_risks: dict[str, FileRisk],
    repo_name: str = "",
) -> dict:
    """Convert flat file risk map into nested directory tree for D3 treemap.

    Returns a nested dict: {name, children: [{name, children|value, risk_score, ...}]}
    """
    root: dict = {"name": repo_name or "root", "children": []}

    for fpath, risk in sorted(file_risks.items()):
        parts = fpath.split("/")
        node = root

        # Navigate/create directory nodes
        for part in parts[:-1]:
            child = next((c for c in node["children"] if c["name"] == part), None)
            if child is None:
                child = {"name": part, "children": []}
                node["children"].append(child)
            node = child

        # Add leaf node (file)
        node["children"].append({
            "name": parts[-1],
            "full_path": fpath,
            "value": max(risk.lines, 100),  # minimum size for visibility
            "risk_score": round(risk.risk_score, 3),
            "hit_count": risk.hit_count,
            "max_severity": risk.max_severity,
            "patterns": risk.patterns,
            "confirmed_bugs": risk.confirmed_bugs,
            "findings_count": len(risk.findings),
            "findings_sample": risk.findings[:5],  # limit for HTML size
        })

    return root


def save_aggregate(
    file_risks: dict[str, FileRisk],
    output_path: str | Path,
) -> None:
    """Save aggregated results to JSON."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    for fpath, risk in sorted(file_risks.items(), key=lambda x: x[1].risk_score, reverse=True):
        data[fpath] = {
            "hit_count": risk.hit_count,
            "max_severity": risk.max_severity,
            "risk_score": round(risk.risk_score, 3),
            "patterns": risk.patterns,
            "confirmed_bugs": risk.confirmed_bugs,
            "findings_count": len(risk.findings),
        }

    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
