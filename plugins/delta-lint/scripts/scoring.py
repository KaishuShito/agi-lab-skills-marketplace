"""
Scoring configuration for delta-lint.

Single source of truth for all scoring weights.
Defaults are hardcoded here; teams can override via .delta-lint/config.json.

Scale design:
    debt_score:  0 〜 1000  (severity × pattern × status × DEBT_SCALE)
    roi_score:   0 〜 数千   (severity × churn × fan_out / fix_cost × ROI_SCALE)
    info_score:  0 〜 数千   (surprise × entropy × channel / fix_cost × INFO_SCALE)

    大きい数字のほうが直感的。「負債 600」「解消価値 3500」。

Usage:
    from scoring import load_scoring_config

    cfg = load_scoring_config("/path/to/repo")
    cfg.severity_weight["high"]   # → 1.0
    cfg.pattern_weight["①"]      # → 1.0
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Scale multipliers — 各スコアの出力レンジを制御
# ---------------------------------------------------------------------------

DEBT_SCALE = 1000       # debt_score: 0〜1000
ROI_SCALE = 100         # roi_score: 0〜数千（churn/fan_out が大きいと数千に達する）
INFO_SCALE = 100        # info_score: 0〜数千（同上）

# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------

DEFAULT_SEVERITY_WEIGHT: dict[str, float] = {
    "high": 1.0,
    "medium": 0.6,
    "low": 0.3,
}

DEFAULT_PATTERN_WEIGHT: dict[str, float] = {
    "①": 1.0,
    "②": 1.0,
    "③": 0.9,
    "④": 1.0,
    "⑤": 0.8,
    "⑥": 0.9,
    "⑦": 0.3,
    "⑧": 0.6,
    "⑨": 0.5,
    "⑩": 0.4,
}

DEFAULT_STATUS_MULTIPLIER: dict[str, float] = {
    "found": 1.0,
    "verified": 1.0,
    "submitted": 0.8,
    "merged": 0.0,
    "rejected": 0.5,
    "wontfix": 0.0,
    "duplicate": 0.0,
}

# ---------------------------------------------------------------------------
# ROI weights — used by 解消価値 = severity × churn × fan_out / fix_cost
# ---------------------------------------------------------------------------

# Churn: normalized from git log change count (last 6 months)
# 小規模リポでも差が出るように hot=3/月 に引き下げ。
# 大規模リポ（月10回以上）は cap されるが、weight は十分大きい。
DEFAULT_CHURN_THRESHOLDS: dict[str, float] = {
    "hot": 3.0,        # changes/month >= hot → max weight
    "warm": 0.5,       # changes/month >= warm → 中間
    "cold": 0.0,       # changes/month < warm → min weight
    "max_weight": 10.0,
    "min_weight": 0.5,
}

# Fan-out: number of files that import/reference this file
# 5参照で max に到達。3参照でも weight 7.0 — 小規模リポで差が出る。
DEFAULT_FAN_OUT_THRESHOLDS: dict[str, float] = {
    "high": 5.0,       # fan_out >= high → max weight
    "medium": 2.0,     # fan_out >= medium → 中間
    "low": 0.0,        # fan_out < medium → min weight
    "max_weight": 10.0,
    "min_weight": 1.0,
}

# Fix cost: パターン別の修正工数。
# 範囲: 0.5（削除だけ）〜 8.0（大規模リファクタ）。
# 実際のコスト（テスト壊れ、他チーム影響）は finding 単位で上書き可能。
DEFAULT_FIX_COST: dict[str, float] = {
    "①": 1.5,   # Asymmetric Defaults — デフォルト値の統一
    "②": 2.0,   # Semantic Mismatch — 意味の統一は影響範囲が広い
    "③": 1.5,   # External Spec Divergence — 仕様準拠修正
    "④": 1.0,   # Guard Non-Propagation — ガード追加だけ
    "⑤": 2.5,   # Paired-Setting Override — 設定の整合性は波及する
    "⑥": 2.0,   # Lifecycle Ordering — 実行順序の修正
    "⑦": 0.5,   # Dead Code — 削除するだけ
    "⑧": 3.0,   # Duplication Drift — 共通化が必要
    "⑨": 1.5,   # Interface Mismatch — シグネチャ修正 + 呼び出し側
    "⑩": 5.0,   # Missing Abstraction — 共通ユーティリティ作成 + 全箇所移行
    "_default": 1.5,
}


@dataclass
class ScoringConfig:
    """Resolved scoring weights (defaults merged with team overrides)."""

    severity_weight: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SEVERITY_WEIGHT))
    pattern_weight: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PATTERN_WEIGHT))
    status_multiplier: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_STATUS_MULTIPLIER))
    churn_thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CHURN_THRESHOLDS))
    fan_out_thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_FAN_OUT_THRESHOLDS))
    fix_cost: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_FIX_COST))

    def to_dict(self) -> dict:
        """Serialize to dict for config.json export."""
        return {
            "severity_weight": dict(self.severity_weight),
            "pattern_weight": dict(self.pattern_weight),
            "status_multiplier": dict(self.status_multiplier),
            "churn_thresholds": dict(self.churn_thresholds),
            "fan_out_thresholds": dict(self.fan_out_thresholds),
            "fix_cost": dict(self.fix_cost),
        }


def _merge_weights(defaults: dict[str, float], overrides: dict) -> dict[str, float]:
    """Merge team overrides into defaults. Unknown keys are added (forward-compat)."""
    merged = dict(defaults)
    for k, v in overrides.items():
        try:
            merged[k] = float(v)
        except (TypeError, ValueError):
            pass  # skip invalid values silently
    return merged


def load_scoring_config(repo_path: str | Path = ".") -> ScoringConfig:
    """Load scoring config from .delta-lint/config.json.

    Reads the "scoring" section and merges each sub-key with defaults.
    Missing keys use defaults. Unknown keys are preserved (forward-compat
    for Phase 2: likelihood_weight, blast_radius_weight).
    """
    config_path = Path(repo_path).resolve() / ".delta-lint" / "config.json"
    scoring_overrides: dict = {}

    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            scoring_overrides = data.get("scoring", {})
        except (OSError, json.JSONDecodeError):
            pass

    return ScoringConfig(
        severity_weight=_merge_weights(
            DEFAULT_SEVERITY_WEIGHT,
            scoring_overrides.get("severity_weight", {}),
        ),
        pattern_weight=_merge_weights(
            DEFAULT_PATTERN_WEIGHT,
            scoring_overrides.get("pattern_weight", {}),
        ),
        status_multiplier=_merge_weights(
            DEFAULT_STATUS_MULTIPLIER,
            scoring_overrides.get("status_multiplier", {}),
        ),
        churn_thresholds=_merge_weights(
            DEFAULT_CHURN_THRESHOLDS,
            scoring_overrides.get("churn_thresholds", {}),
        ),
        fan_out_thresholds=_merge_weights(
            DEFAULT_FAN_OUT_THRESHOLDS,
            scoring_overrides.get("fan_out_thresholds", {}),
        ),
        fix_cost=_merge_weights(
            DEFAULT_FIX_COST,
            scoring_overrides.get("fix_cost", {}),
        ),
    )


def export_default_config() -> dict:
    """Return the full default scoring config as a dict.

    Used by `config init` to write a starter config.json.
    """
    return ScoringConfig().to_dict()


def diff_from_defaults(cfg: ScoringConfig) -> dict[str, dict[str, tuple[float, float]]]:
    """Return keys where team config differs from defaults.

    Returns: {"severity_weight": {"high": (default, custom)}, ...}
    Only includes keys with different values.
    """
    defaults = {
        "severity_weight": DEFAULT_SEVERITY_WEIGHT,
        "pattern_weight": DEFAULT_PATTERN_WEIGHT,
        "status_multiplier": DEFAULT_STATUS_MULTIPLIER,
        "churn_thresholds": DEFAULT_CHURN_THRESHOLDS,
        "fan_out_thresholds": DEFAULT_FAN_OUT_THRESHOLDS,
        "fix_cost": DEFAULT_FIX_COST,
    }
    current = {
        "severity_weight": cfg.severity_weight,
        "pattern_weight": cfg.pattern_weight,
        "status_multiplier": cfg.status_multiplier,
        "churn_thresholds": cfg.churn_thresholds,
        "fan_out_thresholds": cfg.fan_out_thresholds,
        "fix_cost": cfg.fix_cost,
    }
    diffs: dict[str, dict[str, tuple[float, float]]] = {}
    for section, default_dict in defaults.items():
        cur = current[section]
        section_diff: dict[str, tuple[float, float]] = {}
        # Check modified + new keys
        for k, v in cur.items():
            dv = default_dict.get(k)
            if dv is None or abs(v - dv) > 1e-9:
                section_diff[k] = (dv, v)  # (default_or_None, custom)
        if section_diff:
            diffs[section] = section_diff
    return diffs


def validate_config(cfg: ScoringConfig) -> list[str]:
    """Validate scoring config. Returns list of warning messages."""
    warnings: list[str] = []
    known = {
        "severity_weight": set(DEFAULT_SEVERITY_WEIGHT.keys()),
        "pattern_weight": set(DEFAULT_PATTERN_WEIGHT.keys()),
        "status_multiplier": set(DEFAULT_STATUS_MULTIPLIER.keys()),
    }
    current = {
        "severity_weight": cfg.severity_weight,
        "pattern_weight": cfg.pattern_weight,
        "status_multiplier": cfg.status_multiplier,
    }
    for section, known_keys in known.items():
        for k in current[section]:
            if k not in known_keys:
                warnings.append(f"{section}.{k}: 未知のキー（タイポ？）")
    for section, cur in current.items():
        for k, v in cur.items():
            if v < 0:
                warnings.append(f"{section}.{k}: 負の値 ({v})")
    return warnings


# ---------------------------------------------------------------------------
# ROI (解消価値) computation
# ---------------------------------------------------------------------------

def churn_to_weight(changes_6m: int, cfg: ScoringConfig | None = None) -> float:
    """Convert raw git churn (change count in 6 months) to weight.

    Linear interpolation between min_weight and max_weight.
    hot=3/月 → 6ヶ月で18回変更で max。小規模リポでも差が出る。
    """
    t = (cfg or ScoringConfig()).churn_thresholds
    max_w = t.get("max_weight", 10.0)
    min_w = t.get("min_weight", 0.5)
    hot = t.get("hot", 3.0) * 6  # hot is per-month, convert to 6-month total
    if hot <= 0:
        return min_w
    ratio = min(changes_6m / hot, 1.0)
    return round(min_w + (max_w - min_w) * ratio, 2)


def fan_out_to_weight(fan_out: int, cfg: ScoringConfig | None = None) -> float:
    """Convert raw fan-out (import reference count) to weight.

    high=5 で max。3参照でも weight 6.4 — 小規模リポで十分な差。
    """
    t = (cfg or ScoringConfig()).fan_out_thresholds
    max_w = t.get("max_weight", 10.0)
    min_w = t.get("min_weight", 1.0)
    high = t.get("high", 5.0)
    if high <= 0:
        return min_w
    ratio = min(fan_out / high, 1.0)
    return round(min_w + (max_w - min_w) * ratio, 2)


def pattern_fix_cost(pattern: str, cfg: ScoringConfig | None = None) -> float:
    """Get fix cost weight for a contradiction pattern."""
    fc = (cfg or ScoringConfig()).fix_cost
    return fc.get(pattern, fc.get("_default", 1.5))


def compute_roi(
    severity: str,
    churn_6m: int,
    fan_out: int,
    pattern: str,
    cfg: ScoringConfig | None = None,
    fix_churn_6m: int | None = None,
) -> dict:
    """Compute 解消価値 (resolution ROI) for a finding.

    Formula: severity × churn_weight × fan_out_weight / fix_cost × ROI_SCALE

    churn_weight は fix_churn_6m（バグ修正コミット数）があればそちらを優先使用。
    fix_churn はバグ修正に関連するコミットのみカウントするため、
    機能追加で膨らんだ churn よりも「壊れやすさ」の精度が高い。

    出力レンジ: 0 〜 数千。
    典型例:
      high severity + hot fix_churn + 5 fan_out + ④ガード追加 → ~10000
      low severity + cold churn + 1 fan_out + ⑩共通化         → ~3
    """
    c = cfg or ScoringConfig()
    sev_w = c.severity_weight.get(severity, 0.3)

    # fix_churn_6m があればそちらで churn_weight を計算（精度が高い）
    effective_churn = fix_churn_6m if fix_churn_6m is not None else churn_6m
    churn_w = churn_to_weight(effective_churn, c)

    fan_w = fan_out_to_weight(fan_out, c)
    fix_c = pattern_fix_cost(pattern, c)

    score = round(sev_w * churn_w * fan_w / max(fix_c, 0.1) * ROI_SCALE, 1)

    return {
        "churn_6m": churn_6m,
        "fix_churn_6m": fix_churn_6m,
        "churn_weight": churn_w,
        "fan_out": fan_out,
        "fan_out_weight": fan_w,
        "fix_cost": fix_c,
        "roi_score": score,
    }
