"""
Scoring configuration for delta-lint.

Single source of truth for all scoring weights.
Defaults are hardcoded here; teams can override via .delta-lint/config.json.

Scale design:
    debt_coefficient: 0 〜 1.0  (severity × pattern × status — 静的な重さ)
    context_score:    0 〜 数千  (churn × fan_out × user_facing × age / fix_cost × ROI_SCALE — 文脈)
    technical_debt:   0 〜 数千  (debt_coefficient × context_score — 統合指標)
    info_score:       0 〜 数千  (discovery_value × concentration_factor × INFO_SCALE)

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

ROI_SCALE = 100         # context_score: 0〜数千（churn/fan_out が大きいと数千に達する）
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
    "confirmed": 1.0,
    "suspicious": 0.9,
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
# 対数スケール: log₂(1 + fan_out) / log₂(1 + high) で天井到達を遅らせる。
# fan_out=5 → 3.6, fan_out=20 → 6.8, fan_out=50 → 8.5, fan_out=100 → 9.5
DEFAULT_FAN_OUT_THRESHOLDS: dict[str, float] = {
    "high": 100.0,     # fan_out >= high → max weight (対数なので緩やか)
    "medium": 5.0,     # fan_out >= medium → 中間
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

# ---------------------------------------------------------------------------
# User-facing weight — ユーザーに直接見える問題は優先度を上げる
# ---------------------------------------------------------------------------
DEFAULT_USER_FACING_WEIGHT: dict[str, float] = {
    "user_facing": 1.5,    # UI表示、エラーメッセージ、API応答に影響
    "internal": 1.0,       # 内部ロジック、ログ、設定
    "_default": 1.0,
}

# ---------------------------------------------------------------------------
# Age acceleration — 放置コストの複利効果
# 経過日数 → 加速係数。30日で×1、90日で×1.6、180日で×2、365日で×2.5
# log(1 + days/30) ベース。config.json で base_days / max_multiplier を上書き可能。
# ---------------------------------------------------------------------------
DEFAULT_AGE_ACCELERATION: dict[str, float] = {
    "base_days": 30.0,       # この日数で加速係数 1.0（ベースライン）
    "max_multiplier": 3.0,   # 上限。無限に上がらないようにキャップ
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
    user_facing_weight: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_USER_FACING_WEIGHT))
    age_acceleration: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_AGE_ACCELERATION))

    def to_dict(self) -> dict:
        """Serialize to dict for config.json export."""
        return {
            "severity_weight": dict(self.severity_weight),
            "pattern_weight": dict(self.pattern_weight),
            "status_multiplier": dict(self.status_multiplier),
            "churn_thresholds": dict(self.churn_thresholds),
            "fan_out_thresholds": dict(self.fan_out_thresholds),
            "fix_cost": dict(self.fix_cost),
            "user_facing_weight": dict(self.user_facing_weight),
            "age_acceleration": dict(self.age_acceleration),
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


def load_scoring_config(
    repo_path: str | Path = ".",
    profile_overrides: dict | None = None,
) -> ScoringConfig:
    """Load scoring config with 3-tier merge.

    Priority: profile > config.json > defaults

    Args:
        repo_path: Repository path for .delta-lint/config.json
        profile_overrides: scoring_weights from profile policy (highest priority)

    Reads the "scoring" section from config.json, then merges profile
    overrides on top. Missing keys use defaults. Unknown keys are
    preserved (forward-compat).
    """
    def _load_json_safe(p: Path) -> dict:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {}

    global_scoring = _load_json_safe(
        Path.home() / ".delta-lint" / "config.json"
    ).get("scoring", {})
    local_scoring = _load_json_safe(
        Path(repo_path).resolve() / ".delta-lint" / "config.json"
    ).get("scoring", {})
    scoring_overrides = _merge_weights(global_scoring, local_scoring)

    # 4-tier merge: defaults ← global config ← repo config ← profile
    def _resolve(key: str, defaults: dict) -> dict:
        merged = _merge_weights(defaults, scoring_overrides.get(key, {}))
        if profile_overrides and key in profile_overrides:
            merged = _merge_weights(merged, profile_overrides[key])
        return merged

    return ScoringConfig(
        severity_weight=_resolve("severity_weight", DEFAULT_SEVERITY_WEIGHT),
        pattern_weight=_resolve("pattern_weight", DEFAULT_PATTERN_WEIGHT),
        status_multiplier=_resolve("status_multiplier", DEFAULT_STATUS_MULTIPLIER),
        churn_thresholds=_resolve("churn_thresholds", DEFAULT_CHURN_THRESHOLDS),
        fan_out_thresholds=_resolve("fan_out_thresholds", DEFAULT_FAN_OUT_THRESHOLDS),
        fix_cost=_resolve("fix_cost", DEFAULT_FIX_COST),
        user_facing_weight=_resolve("user_facing_weight", DEFAULT_USER_FACING_WEIGHT),
        age_acceleration=_resolve("age_acceleration", DEFAULT_AGE_ACCELERATION),
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
        "user_facing_weight": DEFAULT_USER_FACING_WEIGHT,
        "age_acceleration": DEFAULT_AGE_ACCELERATION,
    }
    current = {
        "severity_weight": cfg.severity_weight,
        "pattern_weight": cfg.pattern_weight,
        "status_multiplier": cfg.status_multiplier,
        "churn_thresholds": cfg.churn_thresholds,
        "fan_out_thresholds": cfg.fan_out_thresholds,
        "fix_cost": cfg.fix_cost,
        "user_facing_weight": cfg.user_facing_weight,
        "age_acceleration": cfg.age_acceleration,
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

    対数スケール: log₂(1 + churn) / log₂(1 + hot_6m) で穏やかにスケール。
    churn=1 → 1.5, churn=5 → 3.7, churn=18 → 7.3, churn=36+ → 10.0
    """
    import math
    t = (cfg or ScoringConfig()).churn_thresholds
    max_w = t.get("max_weight", 10.0)
    min_w = t.get("min_weight", 0.5)
    hot = t.get("hot", 3.0) * 6  # hot is per-month, convert to 6-month total
    if hot <= 0 or changes_6m <= 0:
        return min_w
    ratio = min(math.log2(1 + changes_6m) / math.log2(1 + hot), 1.0)
    return round(min_w + (max_w - min_w) * ratio, 2)


def fan_out_to_weight(fan_out: int, cfg: ScoringConfig | None = None) -> float:
    """Convert raw fan-out (import reference count) to weight.

    対数スケール: log₂(1 + fan_out) / log₂(1 + high) で天井到達を遅らせる。
    fan_out=1 → 1.8, fan_out=5 → 3.6, fan_out=20 → 6.8, fan_out=50 → 8.5
    """
    import math
    t = (cfg or ScoringConfig()).fan_out_thresholds
    max_w = t.get("max_weight", 10.0)
    min_w = t.get("min_weight", 1.0)
    high = t.get("high", 100.0)
    if high <= 0 or fan_out <= 0:
        return min_w
    ratio = min(math.log2(1 + fan_out) / math.log2(1 + high), 1.0)
    return round(min_w + (max_w - min_w) * ratio, 2)


def pattern_fix_cost(pattern: str, cfg: ScoringConfig | None = None) -> float:
    """Get fix cost weight for a contradiction pattern."""
    fc = (cfg or ScoringConfig()).fix_cost
    return fc.get(pattern, fc.get("_default", 1.5))


def user_facing_to_weight(user_facing: bool, cfg: ScoringConfig | None = None) -> float:
    """ユーザーに直接見える問題かどうかで重みを返す。

    user_facing=True → UI/エラーメッセージ/API応答に影響 → 1.5x
    user_facing=False → 内部ロジック → 1.0x
    """
    w = (cfg or ScoringConfig()).user_facing_weight
    if user_facing:
        return w.get("user_facing", 1.5)
    return w.get("internal", w.get("_default", 1.0))


def age_to_multiplier(found_at: str, churn_6m: int = 0, cfg: ScoringConfig | None = None) -> float:
    """放置期間 × 周辺活発度から加速係数を計算。

    「古い」だけでは加速しない。周辺が活発に変更されているのに
    この finding だけ放置されている = 地雷化 → 加速。
    churn=0（誰も触らないファイル）なら age は効かない。

    formula: 1 + log₂(1 + days / base_days) × churn_ratio
    churn_ratio = min(churn_6m / hot_threshold, 1.0)
    """
    import math
    if not found_at or churn_6m <= 0:
        return 1.0
    try:
        from datetime import datetime
        found = datetime.strptime(found_at[:10], "%Y-%m-%d")
        now = datetime.now()
        days = max((now - found).days, 0)
    except (ValueError, TypeError):
        return 1.0

    a = (cfg or ScoringConfig()).age_acceleration
    base_days = a.get("base_days", 30.0)
    max_mult = a.get("max_multiplier", 3.0)
    if base_days <= 0:
        return 1.0

    # churn が高いほど age の効きが強い（周辺活発 × 放置 = 地雷）
    c = (cfg or ScoringConfig()).churn_thresholds
    hot = c.get("hot", 3.0) * 6
    churn_ratio = min(churn_6m / max(hot, 1), 1.0)

    raw_age = math.log2(1 + days / base_days)
    mult = 1.0 + raw_age * churn_ratio
    return round(min(mult, max_mult), 2)


def debt_coefficient(
    severity: str,
    pattern: str,
    status: str = "found",
    cfg: ScoringConfig | None = None,
) -> float:
    """負債係数: severity × pattern × status → 0〜1.0。

    静的な重さ。文脈（churn, fan_out 等）は含まない。
    """
    c = cfg or ScoringConfig()
    sev = c.severity_weight.get(severity, 0.3)
    pat = c.pattern_weight.get(pattern, 0.5)
    sta = c.status_multiplier.get(status, 1.0)
    return round(sev * pat * sta, 3)


def compute_roi(
    severity: str,
    churn_6m: int,
    fan_out: int,
    pattern: str,
    cfg: ScoringConfig | None = None,
    fix_churn_6m: int | None = None,
    user_facing: bool = False,
    found_at: str = "",
    status: str = "found",
) -> dict:
    """Compute context score and technical debt for a finding.

    context_score = churn × fan_out × user_facing × age / fix_cost × ROI_SCALE
    debt_coefficient = severity × pattern × status  (0〜1.0)
    technical_debt = debt_coefficient × context_score

    context_score は文脈のみ（ファイルの活発度、影響範囲、修正コスト）。
    debt_coefficient は静的な重さ（深刻度、パターン、ステータス）。
    掛け合わせた technical_debt が統合指標。
    """
    c = cfg or ScoringConfig()

    # fix_churn_6m があればそちらで churn_weight を計算（精度が高い）
    effective_churn = fix_churn_6m if fix_churn_6m is not None else churn_6m
    churn_w = churn_to_weight(effective_churn, c)

    fan_w = fan_out_to_weight(fan_out, c)
    fix_c = pattern_fix_cost(pattern, c)
    uf_w = user_facing_to_weight(user_facing, c)
    age_w = age_to_multiplier(found_at, effective_churn, c)

    context = round(churn_w * fan_w * uf_w * age_w / max(fix_c, 0.1) * ROI_SCALE, 1)
    dc = debt_coefficient(severity, pattern, status, c)
    tech_debt = round(dc * context, 1)

    return {
        "churn_6m": churn_6m,
        "fix_churn_6m": fix_churn_6m,
        "churn_weight": churn_w,
        "fan_out": fan_out,
        "fan_out_weight": fan_w,
        "fix_cost": fix_c,
        "user_facing": user_facing,
        "user_facing_weight": uf_w,
        "age_days": 0,  # filled by caller if available
        "age_multiplier": age_w,
        "debt_coefficient": dc,
        "context_score": context,
        "roi_score": tech_debt,  # 後方互換: roi_score キーを維持
    }
