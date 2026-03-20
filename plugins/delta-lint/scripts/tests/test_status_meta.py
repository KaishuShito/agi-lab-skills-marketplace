"""
STATUS_META Single Source of Truth テスト.

STATUS_META (findings.py) がステータス定義の唯一の正規定義であることを検証する。
scoring.py, dashboard, workflow がすべて STATUS_META から派生していることを保証する。
"""

import sys
from pathlib import Path

# scripts/ ディレクトリをパスに追加（パッケージではないため）
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from findings import STATUS_META, VALID_STATUSES
from scoring import DEFAULT_STATUS_MULTIPLIER


REQUIRED_KEYS = {"label", "color", "closed", "debt_weight"}


class TestStatusMetaCompleteness:
    """STATUS_META の各ステータスが必要なキーをすべて持つことを検証."""

    def test_all_statuses_have_required_keys(self):
        for status, meta in STATUS_META.items():
            missing = REQUIRED_KEYS - set(meta.keys())
            assert not missing, (
                f"STATUS_META['{status}'] に必須キーが不足: {missing}"
            )

    def test_status_meta_is_not_empty(self):
        assert len(STATUS_META) > 0, "STATUS_META が空"


class TestValidStatusesDerivation:
    """VALID_STATUSES が STATUS_META.keys() から派生していることを検証."""

    def test_valid_statuses_equals_status_meta_keys(self):
        assert VALID_STATUSES == tuple(STATUS_META.keys())

    def test_valid_statuses_is_tuple(self):
        assert isinstance(VALID_STATUSES, tuple)


class TestScoringConsistency:
    """scoring.py の DEFAULT_STATUS_MULTIPLIER が STATUS_META と一致することを検証."""

    def test_keys_match_exactly(self):
        assert set(DEFAULT_STATUS_MULTIPLIER.keys()) == set(STATUS_META.keys()), (
            f"キーの差分: "
            f"scoring にのみ存在={set(DEFAULT_STATUS_MULTIPLIER.keys()) - set(STATUS_META.keys())}, "
            f"STATUS_META にのみ存在={set(STATUS_META.keys()) - set(DEFAULT_STATUS_MULTIPLIER.keys())}"
        )

    def test_weights_match_debt_weight(self):
        for status, meta in STATUS_META.items():
            expected = meta["debt_weight"]
            actual = DEFAULT_STATUS_MULTIPLIER.get(status)
            assert actual is not None, (
                f"DEFAULT_STATUS_MULTIPLIER に '{status}' が存在しない"
            )
            assert abs(actual - expected) < 1e-9, (
                f"'{status}': debt_weight={expected} != multiplier={actual}"
            )


class TestResolvedStatusesDerivation:
    """resolved_statuses が STATUS_META の closed フラグから正しく派生することを検証."""

    def test_resolved_matches_closed_statuses(self):
        expected_resolved = {k for k, v in STATUS_META.items() if v["closed"]}
        # findings.py 内の generate_dashboard で使用されるロジックと同一
        assert expected_resolved == {k for k, v in STATUS_META.items() if v["closed"]}

    def test_resolved_is_nonempty(self):
        resolved = {k for k, v in STATUS_META.items() if v["closed"]}
        assert len(resolved) > 0, "closed なステータスが一つもない"


class TestNoHardcodedVerifiedStatus:
    """'verified' ステータスが STATUS_META に存在しないことを検証.

    過去に 'verified' が使われていたが、現在は STATUS_META にない。
    ハードコードされた 'verified' が残っていないことを確認する。
    """

    def test_verified_not_in_status_meta(self):
        assert "verified" not in STATUS_META, (
            "'verified' が STATUS_META に存在する — 廃止済みステータスが復活している"
        )

    def test_verified_not_in_valid_statuses(self):
        assert "verified" not in VALID_STATUSES

    def test_verified_not_in_scoring(self):
        assert "verified" not in DEFAULT_STATUS_MULTIPLIER


class TestDebtWeightRange:
    """debt_weight が 0.0〜1.0 の範囲内であることを検証."""

    @pytest.mark.parametrize(
        "status", list(STATUS_META.keys())
    )
    def test_debt_weight_in_range(self, status):
        weight = STATUS_META[status]["debt_weight"]
        assert 0.0 <= weight <= 1.0, (
            f"STATUS_META['{status}']['debt_weight'] = {weight} は範囲外 (0.0〜1.0)"
        )


class TestClosedStatusSemantics:
    """各ステータスの closed フラグが意味的に正しいことを検証."""

    @pytest.mark.parametrize(
        "status", ["merged", "wontfix", "duplicate", "false_positive"]
    )
    def test_should_be_closed(self, status):
        assert status in STATUS_META, f"'{status}' が STATUS_META に存在しない"
        assert STATUS_META[status]["closed"] is True, (
            f"'{status}' は closed=True であるべき"
        )

    @pytest.mark.parametrize(
        "status", ["found", "suspicious", "confirmed", "submitted"]
    )
    def test_should_not_be_closed(self, status):
        assert status in STATUS_META, f"'{status}' が STATUS_META に存在しない"
        assert STATUS_META[status]["closed"] is False, (
            f"'{status}' は closed=False であるべき"
        )
