"""
Aggregation layer for delta-lint parallel multi-agent mode.

Responsible for:
- Merging findings from multiple agents
- Deduplicating by (file_a, file_b, pattern)
- Detecting root cause chains (same file in multiple findings)
- Removing agent errors from final output
"""

from collections import Counter


def aggregate_findings(all_findings: list[dict], verbose: bool = False) -> list[dict]:
    """Merge, deduplicate, and enrich findings from parallel agents.

    Args:
        all_findings: Raw combined findings from all agents
        verbose: Print dedup stats to stderr

    Returns:
        Deduplicated findings list, sorted by severity
    """
    # Separate errors from real findings
    errors = [f for f in all_findings if f.get("_agent_error")]
    findings = [f for f in all_findings if not f.get("_agent_error")
                and not f.get("parse_error")]
    parse_errors = [f for f in all_findings if f.get("parse_error")]

    # Deduplicate
    deduped = _deduplicate(findings)

    # Root cause chain detection
    deduped = _detect_root_cause_chains(deduped)

    # Re-add parse errors (keep them visible)
    deduped.extend(parse_errors)

    # Sort: high first, then findings with root_cause_score
    severity_order = {"high": 0, "medium": 1, "low": 2}
    deduped.sort(key=lambda f: (
        severity_order.get(f.get("severity", "medium").lower(), 1),
        -f.get("_root_cause_score", 0),
    ))

    if verbose:
        import sys
        total = len(findings)
        after = len([f for f in deduped if not f.get("parse_error")])
        print(f"  Aggregation: {total} raw → {after} deduplicated "
              f"({total - after} duplicates removed)", file=sys.stderr)
        if errors:
            print(f"  Agent errors: {len(errors)}", file=sys.stderr)

    return deduped


def _deduplicate(findings: list[dict]) -> list[dict]:
    """Remove duplicate findings based on (file_a, file_b, pattern) key.

    When duplicates exist, keep the one with higher severity.
    """
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    seen: dict[tuple, dict] = {}

    for f in findings:
        loc = f.get("location", {})
        if not isinstance(loc, dict):
            # Keep findings with non-standard location
            key = (str(f.get("contradiction", ""))[:80],)
        else:
            file_a = loc.get("file_a", "")
            file_b = loc.get("file_b", "")
            pattern = f.get("pattern", "")
            # Normalize: sort file pair so (A,B) == (B,A)
            key = (tuple(sorted([file_a, file_b])), str(pattern))

        if key in seen:
            existing = seen[key]
            existing_sev = severity_rank.get(
                existing.get("severity", "medium").lower(), 1)
            new_sev = severity_rank.get(
                f.get("severity", "medium").lower(), 1)
            if new_sev < existing_sev:
                # New finding has higher severity — replace
                seen[key] = f
            elif new_sev == existing_sev:
                # Same severity — merge cluster tags
                existing_cluster = existing.get("_cluster", "")
                new_cluster = f.get("_cluster", "")
                if new_cluster and new_cluster != existing_cluster:
                    existing["_found_by"] = [existing_cluster, new_cluster]
        else:
            seen[key] = f

    return list(seen.values())


def _detect_root_cause_chains(findings: list[dict]) -> list[dict]:
    """Detect files that appear in multiple findings (potential root causes).

    Adds _root_cause_score to findings involving frequently-appearing files.
    """
    # Count file appearances across all findings
    file_counter: Counter = Counter()
    for f in findings:
        loc = f.get("location", {})
        if isinstance(loc, dict):
            for key in ("file_a", "file_b"):
                fpath = loc.get(key, "")
                if fpath:
                    file_counter[fpath] += 1

    # Files appearing in 2+ findings are potential root causes
    hot_files = {fpath for fpath, count in file_counter.items() if count >= 2}

    if not hot_files:
        return findings

    # Score each finding by how many hot files it involves
    for f in findings:
        loc = f.get("location", {})
        if not isinstance(loc, dict):
            continue
        score = 0
        involved_hot = []
        for key in ("file_a", "file_b"):
            fpath = loc.get(key, "")
            if fpath in hot_files:
                score += file_counter[fpath]
                involved_hot.append(fpath)
        if score > 0:
            f["_root_cause_score"] = score
            f["_root_cause_files"] = involved_hot

    return findings
