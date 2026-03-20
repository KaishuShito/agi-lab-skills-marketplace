"""
Information-theoretic helpers for delta-lint.

- **info_score**: discovery_value × concentration_factor × INFO_SCALE
  (newness within the repo × hotspot concentration). Does not use fan_out;
  propagation risk is covered by context_score / ROI in scoring.py.

  Current implementation is a simplified heuristic. Future work: true information
  theory (self-information -log₂ P(finding exists) or conditional entropy reduction
  from fixing this finding). Requires probabilistic model of codebase state.

- **Chao1 / discovery_rate / compute_coverage_from_history**: coverage estimation
  from scan history (how many undiscovered findings may remain).
"""

from __future__ import annotations

import math
from typing import Any

# Closed statuses — same semantics as dashboard / debt_total
_RESOLVED_STATUSES = frozenset({
    "merged", "wontfix", "duplicate", "rejected", "false_positive",
})


def _file_key(f: dict) -> str:
    loc = f.get("location") or {}
    s = (f.get("file") or loc.get("file_a") or "").strip()
    return s or "__empty__"


def _is_open(f: dict) -> bool:
    return f.get("status", "found") not in _RESOLVED_STATUSES


# ---------------------------------------------------------------------------
# Chao1 — 未発見の制約数を推定
# ---------------------------------------------------------------------------


def chao1_estimate(observed: int, singletons: int, doubletons: int) -> dict:
    """Chao1 species richness estimator.

    「発見済み findings から、まだ見つかっていない findings がどれくらいあるか」推定。

    Args:
        observed: 発見済みユニーク findings 数 (S_obs)
        singletons: 1回のスキャンでしか見つかっていない findings (f1)
        doubletons: ちょうど2回のスキャンで見つかった findings (f2)

    Returns:
        dict with estimated_total, coverage_pct, unseen_estimate, ci_lower, ci_upper
    """
    if observed == 0:
        return {
            "estimated_total": 0,
            "coverage_pct": 100,
            "unseen_estimate": 0,
            "ci_lower": 0,
            "ci_upper": 0,
        }

    # Bias-corrected Chao1
    if doubletons == 0:
        unseen = singletons * (singletons - 1) / 2 if singletons > 1 else 0
    else:
        unseen = (singletons ** 2) / (2 * doubletons)

    estimated = observed + unseen
    coverage = round(observed / max(estimated, 1) * 100)

    # 95% CI (log-normal approximation)
    if doubletons > 0:
        var = doubletons * (
            0.25 * (singletons / doubletons) ** 4
            + (singletons / doubletons) ** 3
            + 0.5 * (singletons / doubletons) ** 2
        )
    else:
        var = singletons * (singletons - 1) / 2 + singletons * (2 * singletons - 1) ** 2 / 4

    if var > 0 and unseen > 0:
        c = math.exp(1.96 * math.sqrt(math.log(1 + var / (unseen ** 2))))
        ci_lower = max(observed, round(observed + unseen / c))
        ci_upper = round(observed + unseen * c)
    else:
        ci_lower = observed
        ci_upper = observed

    return {
        "estimated_total": round(estimated),
        "coverage_pct": coverage,
        "unseen_estimate": round(unseen),
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
    }


# ---------------------------------------------------------------------------
# Discovery rate — 発見レート分析
# ---------------------------------------------------------------------------


def discovery_rate(new_per_scan: list[int]) -> dict:
    """スキャンごとの新規 findings 数から発見レートのトレンドを分析。

    Args:
        new_per_scan: 各スキャンで初めて発見された findings 数（時系列順）

    Returns:
        trend: "converging" (減少=カバレッジ向上), "diverging" (増加), "stable"
        ratio: 後半/前半の平均比率
    """
    if len(new_per_scan) < 2:
        return {"trend": "insufficient_data", "scans": len(new_per_scan)}

    mid = len(new_per_scan) // 2
    first_avg = sum(new_per_scan[:mid]) / mid
    second_avg = sum(new_per_scan[mid:]) / (len(new_per_scan) - mid)

    if first_avg == 0:
        ratio = float("inf") if second_avg > 0 else 1.0
    else:
        ratio = second_avg / first_avg

    if ratio < 0.5:
        trend = "converging"
    elif ratio > 1.5:
        trend = "diverging"
    else:
        trend = "stable"

    return {
        "trend": trend,
        "ratio": round(ratio, 2) if ratio != float("inf") else 99.0,
        "first_half_avg": round(first_avg, 1),
        "second_half_avg": round(second_avg, 1),
        "total_scans": len(new_per_scan),
    }


# ---------------------------------------------------------------------------
# Coverage from scan history — スキャン履歴からカバレッジ推定
# ---------------------------------------------------------------------------


def compute_coverage_from_history(
    scan_history: list[dict],
    findings: list[dict],
) -> dict:
    """スキャン履歴と findings から Chao1 カバレッジを計算する。

    scan_history: load_scan_history() の結果
    findings: _get_latest() の結果（各 finding に found_at, id あり）

    finding_ids_per_scan が記録されていなければ、
    タイムスタンプベースで近似する。
    """
    if not scan_history or not findings:
        return {
            "estimated_total": len(findings),
            "coverage_pct": 100 if findings else 0,
            "unseen_estimate": 0,
            "ci_lower": len(findings),
            "ci_upper": len(findings),
            "discovery_trend": "insufficient_data",
            "scans": len(scan_history),
        }

    # --- Per-scan finding IDs が記録されている場合（新方式）---
    has_ids = any("finding_ids" in h for h in scan_history)

    if has_ids:
        detection_count: dict[str, int] = {}  # finding_id → 何回のスキャンで検出されたか
        new_per_scan: list[int] = []
        seen_so_far: set[str] = set()

        for h in scan_history:
            fids = set(h.get("finding_ids", []))
            new_count = len(fids - seen_so_far)
            new_per_scan.append(new_count)
            seen_so_far |= fids
            for fid in fids:
                detection_count[fid] = detection_count.get(fid, 0) + 1

        observed = len(detection_count)
        singletons = sum(1 for c in detection_count.values() if c == 1)
        doubletons = sum(1 for c in detection_count.values() if c == 2)
    else:
        # --- 旧方式互換: findings_count ベースで近似 ---
        observed = len(findings)
        counts = [h.get("findings_count", 0) for h in scan_history]
        total_scans = len(counts)

        if total_scans <= 1:
            # 1回スキャンしただけ → 全部 singleton（保守的）
            singletons = observed
            doubletons = 0
        else:
            # 発見レートの変化から singleton/doubleton を推定
            # 後半で再発見が増える → doubleton が多い（カバレッジ高い）
            mid = total_scans // 2
            first_avg = sum(counts[:mid]) / max(mid, 1)
            second_avg = sum(counts[mid:]) / max(total_scans - mid, 1)

            if first_avg > 0:
                # 後半/前半 比率: <1 なら発見レート減少（カバレッジ収束）
                decay_ratio = second_avg / first_avg
            else:
                decay_ratio = 2.0  # 前半0なら後半で初めて発見 = カバレッジ低い

            # decay_ratio < 1: 収束中 → doubleton 多め（再発見が増えている）
            # decay_ratio > 1: 発散中 → singleton 多め（新規が増えている）
            if decay_ratio <= 1.0:
                # 収束: 全体の ~40% が doubleton
                doubleton_frac = 0.3 + 0.2 * (1.0 - decay_ratio)
            else:
                # 発散: doubleton は少ない（~10-20%）
                doubleton_frac = max(0.05, 0.3 - 0.1 * min(decay_ratio - 1.0, 2.0))

            doubletons = max(1, round(observed * doubleton_frac))
            singletons = max(1, observed - doubletons * 2)

        new_per_scan = counts

    chao = chao1_estimate(observed, singletons, doubletons)
    trend = discovery_rate(new_per_scan)

    return {
        **chao,
        "discovery_trend": trend.get("trend", "insufficient_data"),
        "discovery_ratio": trend.get("ratio", 0),
        "scans": len(scan_history),
    }


# ---------------------------------------------------------------------------
# Information score — repo-relative novelty × hotspot concentration
# ---------------------------------------------------------------------------


def finding_information_score(
    finding: dict[str, Any],
    pattern_history: list[dict] | None = None,
    all_findings: list[dict] | None = None,
) -> dict:
    """Compute info_score from discovery novelty and file hotspot concentration.

    - discovery_value: 1 / sqrt(n) where n = count of findings with same pattern
      in this repo (including self). More repeats of the same pattern → lower.
    - concentration_factor: log2(1 + m) where m = open findings on the same file.
      Hotspots get higher weight. fan_out is intentionally omitted (ROI covers it).

    Args:
        finding: Single finding dict.
        pattern_history: Reserved for future use; ignored.
        all_findings: Full list of findings in the repo for aggregation.
            If None, treats as a single-finding repo (n=m=1).

    Returns:
        dict with discovery_value, concentration_factor, info_score, breakdown.
    """
    from scoring import INFO_SCALE

    pool = all_findings if all_findings is not None else [finding]
    pattern = (finding.get("pattern") or "").strip()
    file_key = _file_key(finding)

    same_pattern_count = sum(
        1 for f in pool
        if (f.get("pattern") or "").strip() == pattern
    )
    if same_pattern_count < 1:
        same_pattern_count = 1
    discovery_value = 1.0 / math.sqrt(same_pattern_count)

    open_in_file = sum(
        1 for f in pool
        if _file_key(f) == file_key and _is_open(f)
    )
    if open_in_file < 1:
        open_in_file = 1
    concentration_factor = math.log2(1 + open_in_file)

    raw = discovery_value * concentration_factor
    score = round(raw * INFO_SCALE, 1)

    return {
        "discovery_value": round(discovery_value, 4),
        "concentration_factor": round(concentration_factor, 4),
        "info_score": score,
        "breakdown": {
            "same_pattern_count": same_pattern_count,
            "open_in_file": open_in_file,
            "discovery_value": discovery_value,
            "concentration_factor": concentration_factor,
        },
    }
