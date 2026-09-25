"""Unit tests for the pure parts of the batch detectors and the alert
ranking — no MongoDB, no network, deterministic input -> known output
(same style as test_zscore.py).
"""

from datetime import datetime, timedelta

import pytest

from app.detectors import forecast_deviation, isolation_forest
from app.detectors.forecast_deviation import (
    DEVIATION_THRESHOLD,
    MIN_TRAINING_HOURS,
    compute_forecast_deviation,
    fit_linear_trend,
)
from app.detectors.hourly_buckets import HourlyBucket
from app.models import Alert, compute_detector_agreement
from app.routers.alerts import _alert_sort_key, _anomaly_alert_severity

HOUR0 = datetime(2026, 3, 1, 0, 0, 0)  # naive-UTC, like MongoDB's $dateTrunc output


def bucket(hour_offset: int, count: int, *, region="EU", brand="Nike", aov=90.0) -> HourlyBucket:
    return HourlyBucket(
        region=region,
        brand=brand,
        hour=HOUR0 + timedelta(hours=hour_offset),
        order_count=count,
        revenue=count * aov,
    )


class TestIsolationForest:
    def _window(self, n=30):
        """n believable hourly buckets: 2-4 orders/hour at ~$90 each."""
        return [bucket(i, 2 + (i % 3)) for i in range(n)]

    def test_runs_without_error_on_a_small_synthetic_window(self):
        buckets = self._window()

        verdicts = isolation_forest.score_buckets(buckets, recent_from=HOUR0)

        assert len(verdicts) == len(buckets)
        for verdict in verdicts.values():
            assert isinstance(verdict.flagged, bool)
            assert 0.0 <= verdict.score <= 1.0

    def test_a_wildly_unusual_bucket_is_flagged_and_normal_ones_are_not(self):
        buckets = self._window() + [bucket(30, 60, aov=250.0)]  # 60 orders, ~3x the usual order value

        verdicts = isolation_forest.score_buckets(buckets, recent_from=HOUR0)

        outlier = verdicts[("EU", "Nike", HOUR0 + timedelta(hours=30))]
        assert outlier.flagged is True
        normal_flags = [v.flagged for k, v in verdicts.items() if k[2] != outlier_hour(30)]
        assert not any(normal_flags)
        # And it's the most anomalous score in the window.
        assert outlier.score == max(v.score for v in verdicts.values())

    def test_a_perfectly_constant_window_flags_nothing(self):
        # Regression: every score is exactly 0.5 here, and sklearn's
        # predict() used to label ALL of these buckets outliers.
        buckets = [bucket(i, 3) for i in range(20)]

        verdicts = isolation_forest.score_buckets(buckets, recent_from=HOUR0)

        assert len(verdicts) == 20
        assert not any(v.flagged for v in verdicts.values())

    def test_same_input_gives_the_same_verdicts(self):
        buckets = self._window() + [bucket(30, 60, aov=250.0)]

        first = isolation_forest.score_buckets(buckets, recent_from=HOUR0)
        second = isolation_forest.score_buckets(buckets, recent_from=HOUR0)

        assert first == second

    def test_only_buckets_from_recent_from_onward_are_scored(self):
        buckets = self._window()

        verdicts = isolation_forest.score_buckets(buckets, recent_from=HOUR0 + timedelta(hours=25))

        assert {k[2] for k in verdicts} == {HOUR0 + timedelta(hours=h) for h in range(25, 30)}

    def test_too_few_buckets_produces_no_verdicts(self):
        buckets = self._window(n=isolation_forest.MIN_BUCKETS - 1)

        assert isolation_forest.score_buckets(buckets, recent_from=HOUR0) == {}


def outlier_hour(offset: int) -> datetime:
    return HOUR0 + timedelta(hours=offset)


class TestForecastDeviationMath:
    def test_a_value_matching_the_history_is_not_flagged(self):
        history = [5.0] * 12

        verdict = compute_forecast_deviation(history, actual=5)

        assert verdict.flagged is False
        assert verdict.expected == pytest.approx(5.0)
        assert verdict.actual == 5.0

    def test_a_deliberately_way_off_value_is_flagged(self):
        history = [5.0] * 12

        verdict = compute_forecast_deviation(history, actual=25)  # 5x the expected 5

        assert verdict.flagged is True
        assert verdict.expected == pytest.approx(5.0)
        assert verdict.actual == 25.0

    def test_a_collapse_is_flagged_too(self):
        verdict = compute_forecast_deviation([10.0] * 12, actual=1)  # 90% below expected

        assert verdict.flagged is True

    def test_threshold_boundary_is_exclusive(self):
        history = [10.0] * 12
        # Exactly 50% above expected is NOT flagged (deviation > threshold, strictly);
        # just past it is.
        assert compute_forecast_deviation(history, actual=15).flagged is False
        assert compute_forecast_deviation(history, actual=15.1).flagged is True
        assert DEVIATION_THRESHOLD == 0.5

    def test_a_custom_threshold_is_respected(self):
        history = [10.0] * 12

        assert compute_forecast_deviation(history, actual=12, threshold=0.1).flagged is True
        assert compute_forecast_deviation(history, actual=12, threshold=0.5).flagged is False

    def test_follows_a_steady_trend_instead_of_flagging_it(self):
        # 1, 2, 3, ... 12: a series that grows by one order every hour. A
        # flat-mean baseline (6.5) would call the natural next value (13)
        # a 100% deviation; the trend line expects exactly 13.
        history = [float(i) for i in range(1, 13)]

        verdict = compute_forecast_deviation(history, actual=13)

        assert verdict.expected == pytest.approx(13.0)
        assert verdict.flagged is False

    def test_too_little_history_gives_no_verdict(self):
        assert compute_forecast_deviation([5.0] * (MIN_TRAINING_HOURS - 1), actual=99) is None

    def test_a_zero_expectation_does_not_divide_by_zero(self):
        verdict = compute_forecast_deviation([0.0] * 12, actual=0)

        assert verdict.flagged is False
        assert verdict.expected == 0.0

    def test_expected_is_floored_at_zero_for_a_steep_downtrend(self):
        history = [float(x) for x in range(12, 0, -1)]  # 12, 11, ... 1

        verdict = compute_forecast_deviation(history, actual=0)

        assert verdict.expected >= 0.0

    def test_fit_linear_trend_recovers_a_known_line(self):
        intercept, slope = fit_linear_trend([3.0, 5.0, 7.0, 9.0])  # y = 3 + 2x

        assert intercept == pytest.approx(3.0)
        assert slope == pytest.approx(2.0)


class TestForecastDeviationScoreBuckets:
    def test_flags_a_flood_bucket_but_not_the_steady_hours_before_it(self):
        buckets = [bucket(i, 3) for i in range(12)] + [bucket(12, 20)]

        verdicts = forecast_deviation.score_buckets(buckets, recent_from=HOUR0)

        flood = verdicts[("EU", "Nike", HOUR0 + timedelta(hours=12))]
        assert flood.flagged is True
        assert flood.expected == pytest.approx(3.0)
        assert flood.actual == 20.0
        steady = [v for k, v in verdicts.items() if k[2] != HOUR0 + timedelta(hours=12)]
        assert steady and not any(v.flagged for v in steady)

    def test_buckets_without_enough_history_get_no_verdict(self):
        buckets = [bucket(i, 3) for i in range(12)]

        verdicts = forecast_deviation.score_buckets(buckets, recent_from=HOUR0)

        # The first MIN_TRAINING_HOURS hours have < 10 hours of history behind them.
        assert {k[2] for k in verdicts} == {
            HOUR0 + timedelta(hours=h) for h in range(MIN_TRAINING_HOURS, 12)
        }

    def test_each_region_brand_series_is_trained_only_on_its_own_history(self):
        quiet = [bucket(i, 2, region="EU") for i in range(12)] + [bucket(12, 2, region="EU")]
        busy = [bucket(i, 50, region="US") for i in range(12)] + [bucket(12, 50, region="US")]

        verdicts = forecast_deviation.score_buckets(quiet + busy, recent_from=HOUR0)

        assert not any(v.flagged for v in verdicts.values())

    def test_zero_order_hours_inside_the_span_count_as_zero_demand(self):
        # Orders only in even hours: the odd hours are real quiet hours
        # (zero-filled), so an even-hour count of 4 is normal for this series.
        buckets = [bucket(h, 4) for h in range(0, 24, 2)]

        verdicts = forecast_deviation.score_buckets(buckets, recent_from=HOUR0)

        assert verdicts, "expected verdicts once 10+ hours of history exist"
        assert all(v.expected < 4.0 for v in verdicts.values())  # zero-fill drags the trend down


class TestDetectorAgreement:
    @pytest.mark.parametrize(
        "z, isolation_forest_flagged, forecast_flagged, expected",
        [
            (False, None, None, 0),
            (True, None, None, 1),  # pre-batch record flagged only by z-score
            (False, False, False, 0),  # batch ran, nothing flagged
            (True, False, False, 1),
            (False, True, False, 1),
            (False, False, True, 1),
            (True, True, False, 2),
            (True, False, True, 2),
            (False, True, True, 2),
            (True, True, True, 3),
            (True, True, None, 2),  # a null detector never counts toward agreement
            (False, None, True, 1),
        ],
    )
    def test_counts_how_many_detectors_flagged(self, z, isolation_forest_flagged, forecast_flagged, expected):
        assert compute_detector_agreement(z, isolation_forest_flagged, forecast_flagged) == expected


def alert(severity, agreement, minutes_ago=0):
    from datetime import timezone

    return Alert(
        type="anomaly",
        severity=severity,
        message="m",
        timestamp=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago),
        related_entity={},
        detector_agreement=agreement,
    )


class TestAlertRanking:
    @pytest.mark.parametrize(
        "z_severity, agreement, expected",
        [
            ("mild", 1, "mild"),
            ("mild", 2, "moderate"),
            ("mild", 3, "severe"),
            ("moderate", 2, "severe"),
            ("severe", 3, "severe"),  # capped
            (None, 1, "mild"),  # IF/forecast-only flag has no z tier
            (None, 2, "moderate"),
        ],
    )
    def test_each_extra_agreeing_detector_raises_severity_one_tier(self, z_severity, agreement, expected):
        assert _anomaly_alert_severity(z_severity, agreement) == expected

    def test_more_agreement_ranks_above_less_within_a_tier_even_if_older(self):
        lone_flag_newer = alert("severe", 1, minutes_ago=0)
        agreed_older = alert("severe", 3, minutes_ago=120)

        ranked = sorted([lone_flag_newer, agreed_older], key=_alert_sort_key)

        assert ranked[0] is agreed_older

    def test_two_detector_agreement_outranks_a_lone_flag_of_the_same_base_severity(self):
        lone = alert(_anomaly_alert_severity("moderate", 1), 1)
        agreed = alert(_anomaly_alert_severity("moderate", 2), 2)

        ranked = sorted([lone, agreed], key=_alert_sort_key)

        assert ranked[0] is agreed

    def test_severity_still_dominates_agreement(self):
        severe_lone = alert("severe", 1)
        moderate_agreed = alert("moderate", 2)

        assert sorted([moderate_agreed, severe_lone], key=_alert_sort_key)[0] is severe_lone

    def test_ties_fall_back_to_newest_first(self):
        older = alert("severe", 3, minutes_ago=30)
        newer = alert("severe", 3, minutes_ago=1)

        assert sorted([older, newer], key=_alert_sort_key)[0] is newer
