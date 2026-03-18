"""
Scoring configuration for delta-lint.

Single source of truth for all scoring weights.
Defaults are hardcoded here; teams can override via .delta-lint/config.json.

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
# Teams can override the normalization thresholds.
DEFAULT_CHURN_THRESHOLDS: dict[str, float] = {
    "hot": 10.0,      # changes/month >= hot → max weight (3.0)
    "warm": 1.0,       # changes/month >= warm → weight 1.0
    "cold": 0.0,       # changes/month < warm → min weight (0.1)
    "max_weight": 3.0,
    "min_weight": 0.1,
}

# Fan-out: number of files that import/reference this file
DEFAULT_FAN_OUT_THRESHOLDS: dict[str, float] = {
    "high": 10.0,      # fan_out >= high → max weight (5.0)
    "medium": 3.0,     # fan_out >= medium → weight 2.0
    "low": 0.0,        # fan_out < medium → weight 1.0
    "max_weight": 5.0,
    "min_weight": 1.0,
}

# Fix cost: mapped from contradiction pattern type (lower = cheaper = higher ROI)
DEFAULT_FIX_COST: dict[str, float] = {
    "①": 1.0,   # Asymmetric Defaults — 比較的安い
    "②": 1.5,   # One-sided Evolution
    "③": 1.0,   # Silent Fallback Divergence
    "④": 0.8,   # Guard Non-Propagation — ガード追加だけ
    "⑤": 1.5,   # Paired-Setting Override
    "⑥": 1.2,   # Lifecycle Ordering
    "⑦": 0.5,   # Dead Code — 削除するだけ
    "⑧": 1.5,   # Duplication Drift — 共通化が必要
    "⑨": 0.8,   # Interface Mismatch — シグネチャ修正
    "⑩": 2.0,   # Missing Abstraction — 共通ユーティリティ作成
    "_default": 1.0,
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
    """
    t = (cfg or ScoringConfig()).churn_thresholds
    max_w = t.get("max_weight", 3.0)
    min_w = t.get("min_weight", 0.1)
    hot = t.get("hot", 10.0) * 6  # hot is per-month, convert to 6-month total
    if hot <= 0:
        return min_w
    ratio = min(changes_6m / hot, 1.0)
    return round(min_w + (max_w - min_w) * ratio, 2)


def fan_out_to_weight(fan_out: int, cfg: ScoringConfig | None = None) -> float:
    """Convert raw fan-out (import reference count) to weight."""
    t = (cfg or ScoringConfig()).fan_out_thresholds
    max_w = t.get("max_weight", 5.0)
    min_w = t.get("min_weight", 1.0)
    high = t.get("high", 10.0)
    if high <= 0:
        return min_w
    ratio = min(fan_out / high, 1.0)
    return round(min_w + (max_w - min_w) * ratio, 2)


def pattern_fix_cost(pattern: str, cfg: ScoringConfig | None = None) -> float:
    """Get fix cost weight for a contradiction pattern."""
    fc = (cfg or ScoringConfig()).fix_cost
    return fc.get(pattern, fc.get("_default", 1.0))


def compute_roi(
    severity: str,
    churn_6m: int,
    fan_out: int,
    pattern: str,
    cfg: ScoringConfig | None = None,
) -> dict:
    """Compute 解消価値 (resolution ROI) for a finding.

    Formula: severity × churn_weight × fan_out_weight / fix_cost

    Returns dict with raw values and computed score for dashboard display.
    """
    c = cfg or ScoringConfig()
    sev_w = c.severity_weight.get(severity, 0.3)
    churn_w = churn_to_weight(churn_6m, c)
    fan_w = fan_out_to_weight(fan_out, c)
    fix_c = pattern_fix_cost(pattern, c)

    score = round(sev_w * churn_w * fan_w / max(fix_c, 0.1), 1)

    return {
        "churn_6m": churn_6m,
        "churn_weight": churn_w,
        "fan_out": fan_out,
        "fan_out_weight": fan_w,
        "fix_cost": fix_c,
        "roi_score": score,
    }
