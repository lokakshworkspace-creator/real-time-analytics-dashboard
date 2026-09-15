"""Unit tests for the z-score detector's pure math core.

Every test here calls compute_zscore() directly with a hand-built
window — no MongoDB, no async, no FastAPI. That's the point of
detectors/zscore.py's score()/compute_zscore() split (see that
module's docstring): the entire decision logic is a pure function of
(window, value), so "deterministic input -> known output" is exactly
what these tests are.
"""

import statistics

import pytest

from app.detectors.zscore import MIN_WINDOW_SIZE, Z_THRESHOLD, compute_zscore

# A believable, tight-spread baseline: 10 points hovering around 50,
# used as the "normal history" for several tests below.
TIGHT_WINDOW = [45, 50, 55, 48, 52, 49, 51, 47, 53, 50]
assert len(TIGHT_WINDOW) == MIN_WINDOW_SIZE  # keep this fixture honest if MIN_WINDOW_SIZE ever changes


class TestNormalValues:
    def test_value_near_the_mean_is_not_flagged(self):
        result = compute_zscore(TIGHT_WINDOW, 51)

        assert result.is_anomaly is False
        assert result.z_score is not None
        assert abs(result.z_score) < Z_THRESHOLD

    def test_value_a_little_outside_the_window_but_under_threshold_is_not_flagged(self):
        # TIGHT_WINDOW's stdev is a few points; a couple of points
        # outside the observed min/max shouldn't be enough to cross
        # |z| > 3 by itself, unlike a wildly extreme value.
        result = compute_zscore(TIGHT_WINDOW, 58)

        assert result.is_anomaly is False
        assert abs(result.z_score) < Z_THRESHOLD


class TestExtremeValues:
    def test_extreme_value_is_flagged_once_past_minimum_window(self):
        result = compute_zscore(TIGHT_WINDOW, 500)

        assert result.is_anomaly is True
        assert result.z_score is not None
        assert result.z_score > Z_THRESHOLD
        assert result.window_size == MIN_WINDOW_SIZE

    def test_extreme_low_value_is_flagged_with_a_negative_zscore(self):
        # Anomalies aren't only "too high" — orders.py's simulated
        # spikes go toward zero on purpose (see simulator.py). |z| is
        # what matters, not the sign.
        result = compute_zscore(TIGHT_WINDOW, -500)

        assert result.is_anomaly is True
        assert result.z_score < -Z_THRESHOLD


class TestColdStartGuard:
    def test_below_minimum_window_never_flags_even_an_absurd_value(self):
        window = [10, 20, 30]  # 3 points, well below MIN_WINDOW_SIZE (10)
        result = compute_zscore(window, 999_999)

        assert result.is_anomaly is False
        assert result.z_score is None
        assert result.window_size == 3

    def test_empty_window_does_not_error(self):
        result = compute_zscore([], 42)

        assert result.is_anomaly is False
        assert result.z_score is None
        assert result.window_size == 0

    def test_exactly_one_below_minimum_still_does_not_activate(self):
        # The precise boundary: MIN_WINDOW_SIZE - 1 prior points is
        # still a cold start; MIN_WINDOW_SIZE exactly is not (see
        # TestExtremeValues above, which uses exactly MIN_WINDOW_SIZE).
        window = TIGHT_WINDOW[:-1]
        assert len(window) == MIN_WINDOW_SIZE - 1

        result = compute_zscore(window, 500)

        assert result.is_anomaly is False
        assert result.z_score is None


class TestConstantWindowGuard:
    def test_perfectly_constant_window_does_not_divide_by_zero(self):
        window = [50] * MIN_WINDOW_SIZE  # stdev == 0 exactly

        # The real assertion here is simply that this doesn't raise
        # ZeroDivisionError — it's called out explicitly because a
        # naive (value - mean) / stdev implementation would crash here.
        result = compute_zscore(window, 999)

        assert result.is_anomaly is False
        assert result.z_score is None
        assert result.window_size == MIN_WINDOW_SIZE

    def test_constant_window_guard_applies_even_to_the_same_value(self):
        window = [7] * MIN_WINDOW_SIZE
        result = compute_zscore(window, 7)  # value matches the constant exactly too

        assert result.is_anomaly is False
        assert result.z_score is None


class TestExactFormula:
    def test_zscore_matches_the_textbook_formula_exactly(self):
        # Independently recomputes mean/stdev/z here (using the same
        # stdlib primitives compute_zscore itself uses) rather than
        # re-deriving the whole algorithm — this is checking that
        # compute_zscore actually *applies* (value - mean) / stdev to
        # its inputs correctly, not re-testing Python's statistics
        # module.
        window = [10, 10, 10, 10, 10, 10, 10, 10, 10, 20]
        value = 50.0

        expected_mean = statistics.mean(window)
        expected_stdev = statistics.stdev(window)
        expected_z = (value - expected_mean) / expected_stdev

        result = compute_zscore(window, value)

        assert result.z_score == pytest.approx(expected_z)
        assert result.is_anomaly == (abs(expected_z) > Z_THRESHOLD)
