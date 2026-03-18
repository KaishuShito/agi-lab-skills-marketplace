"""
Information-theoretic scoring for delta-lint.

Implements concepts from 負債の情報理論.md:
- Surprise: -log₂ P(violation) — rarer violations carry more information
- Coverage: Chao1 estimator for undiscovered constraints
- Discovery rate: trend analysis from scan history
- Information score: unified metric combining surprise, entropy, fan-out

Reference: scoring.py の roi_score はこのモジュールの直感的近似。
"""

import math
from typing import Any


# ---------------------------------------------------------------------------
# 1. Surprise — 自己情報量 -log₂ P(violation)
# ---------------------------------------------------------------------------

# 歴史的パターン発見率（全リポ平均）。スキャン回数が増えると更新される。
# 初期値は delta-lint の 129件分析データから推定。
DEFAULT_PATTERN_RATE: dict[str, float] = {
    "①": 0.15,   # Asymmetric Defaults — 中頻度
    "②": 0.25,   # One-sided Evolution — 最頻出
    "③": 0.08,   # Silent Fallback Divergence — 稀
    "④": 0.30,   # Guard Non-Propagation — 最頻出
    "⑤": 0.10,   # Paired-Setting Override
    "⑥": 0.05,   # Lifecycle Ordering — 稀
    "⑦": 0.20,   # Dead Code
    "⑧": 0.15,   # Duplication Drift
    "⑨": 0.12,   # Interface Mismatch
    "⑩": 0.08,   # Missing Abstraction
}


def surprise(p_violation: float) -> float:
    """Self-information: -log₂ P(violation).

    P が低い（稀な）違反ほどサプライズが大きい = 重大。
    0-1 に正規化（cap: -log₂(0.01) ≈ 6.64）。

    Args:
        p_violation: 0 < p <= 1. この種の違反が発見される確率。

    Returns:
        0.0 (常に見つかる) 〜 1.0 (極めて稀)
    """
    if p_violation <= 0:
        return 1.0
    if p_violation >= 1:
        return 0.0
    raw = -math.log2(p_violation)
    return min(round(raw / 6.64, 3), 1.0)


def surprise_from_pattern(pattern: str, history: list[dict] | None = None) -> float:
    """パターン番号から surprise を計算する。

    history が渡されれば実測の発見率を使う。なければ DEFAULT_PATTERN_RATE。
    """
    if history and len(history) >= 3:
        # 実測: このパターンが見つかったスキャンの割合
        total_scans = len(history)
        scans_with_pattern = sum(
            1 for h in history
            if pattern in h.get("patterns_found", [])
        )
        if scans_with_pattern > 0:
            rate = scans_with_pattern / total_scans
            return surprise(rate)

    rate = DEFAULT_PATTERN_RATE.get(pattern, 0.15)
    return surprise(rate)


# ---------------------------------------------------------------------------
# 2. Chao1 — 未発見の制約数を推定
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
# 3. Discovery rate — 発見レート分析
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
# 4. File entropy — ファイルの不確実性
# ---------------------------------------------------------------------------

def file_entropy(changes_6m: int, total_lines: int) -> float:
    """ファイルの変異率からエントロピーを推定。

    churn が高い（頻繁に変わる）ファイルほど現在の状態の不確実性が高い。
    H(p) = -p log₂(p) - (1-p) log₂(1-p)  (binary entropy)

    Returns:
        0.0 (安定) 〜 1.0 (高変動)
    """
    if total_lines <= 0 or changes_6m <= 0:
        return 0.0

    p = min(changes_6m / total_lines, 1.0)
    if p >= 1.0:
        return 1.0

    q = 1 - p
    h = -p * math.log2(p)
    if q > 0:
        h -= q * math.log2(q)
    return round(h, 3)


# ---------------------------------------------------------------------------
# 5. Information score — 情報理論ベースの統合スコア
# ---------------------------------------------------------------------------

def information_score(
    severity_surprise: float,
    churn_entropy: float,
    fan_out: int,
    fix_cost: float,
) -> float:
    """情報理論ベースの finding スコア。

    score = surprise × (1 + entropy) × log₂(1 + fan_out) / fix_cost

    負債の情報理論.md との対応:
    - surprise     → 自己情報量 -log₂ P(failure)
    - entropy      → ファイルのエントロピー（状態の不確実性）
    - fan_out      → チャネル数（矛盾の伝播経路）
    - fix_cost     → 最小記述に戻すための編集距離

    roi_score との違い:
    - severity を連続的な確率ベースに（high/medium/low ラベルではなく）
    - fan_out に対数スケール適用（10→100 より 0→10 の方が影響大）
    """
    channel_weight = math.log2(1 + max(fan_out, 1))
    raw = severity_surprise * (1 + churn_entropy) * channel_weight / max(fix_cost, 0.1)
    return round(raw, 2)


# ---------------------------------------------------------------------------
# 6. Coverage from scan history — スキャン履歴からカバレッジ推定
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
# 7. Compute information score for a finding dict
# ---------------------------------------------------------------------------

def finding_information_score(
    finding: dict[str, Any],
    pattern_history: list[dict] | None = None,
) -> dict:
    """既存の finding dict から情報理論スコアを計算する。

    Returns:
        dict with surprise, entropy, info_score, and breakdown
    """
    pattern = finding.get("pattern", "")
    sev_surprise = surprise_from_pattern(pattern, pattern_history)

    churn_6m = finding.get("churn_6m", 0)
    total_lines = finding.get("total_lines", 500)  # fallback
    entropy = file_entropy(churn_6m, total_lines)

    fan_out = finding.get("fan_out", 1)
    fix_cost = finding.get("fix_cost", 1.0)

    score = information_score(sev_surprise, entropy, fan_out, fix_cost)

    return {
        "surprise": sev_surprise,
        "entropy": entropy,
        "info_score": score,
        "breakdown": {
            "surprise": sev_surprise,
            "entropy": entropy,
            "fan_out": fan_out,
            "channel_weight": round(math.log2(1 + max(fan_out, 1)), 2),
            "fix_cost": fix_cost,
        },
    }
