"""Forecast-deviation detector: flags an (region, brand, hour) bucket
whose actual order count is far from what a linear trend fitted on that
same series' earlier hours predicted.

Reuses the exact regression behind GET /api/orders/forecast
(`fit_linear_trend`, moved here from routers/orders.py so a detector
doesn't import from a router — routers/orders.py now imports it from
here, so there is still one implementation, not two). What differs is
only the resolution: the forecast endpoint fits daily order counts per
product and projects days ahead; this fits hourly counts per (region,
brand) and projects one hour ahead, to compare against what happened.

Complements the other two detectors rather than duplicating them:
z-score asks "is this hour unusual against the last 24 hours' spread",
Isolation Forest asks "is this combination of order_count/revenue/AOV
unusual", and this asks "is this hour far from where the *trend* said
it would be" — the only one of the three that accounts for a series
that is steadily growing or shrinking, where a flat mean/stdev would
call ordinary growth an anomaly.

Runs in the same batch pass as Isolation Forest (routers/detectors.py),
for the same reason: it needs a whole fitted window, not one write.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from .hourly_buckets import HourlyBucket

# A bucket is flagged when |actual - expected| / max(expected, floor)
# exceeds this. 50% means "an hour that came in at less than half, or
# more than half again above, its trend" — a named constant, not buried
# in the comparison, because it's the one tuning knob a reader will want
# to argue about.
DEVIATION_THRESHOLD = 0.5

# Same cold-start reasoning (and number) as zscore.MIN_WINDOW_SIZE: a
# trend line through a handful of hours is noise, so below this many
# prior hours no verdict is produced (fields stay null).
MIN_TRAINING_HOURS = 10

# Denominator floor. Relative deviation divides by the expected count,
# which for a quiet series can be a fraction of an order — then "1 actual
# vs 0.2 expected" reads as 400% deviation on a single order. Flooring at
# one order means the threshold is only ever measured against at least a
# whole order's worth of baseline.
MIN_EXPECTED_ORDERS = 1.0

_HOUR = timedelta(hours=1)


def fit_linear_trend(y_values: list[float]) -> tuple[float, float]:
    """Ordinary least-squares fit of y = intercept + slope * x, with
    x = 0, 1, 2, ... over the given values in order. Pure Python (sums
    and a division), not a new dependency — "a basic linear regression
    over the recent buckets," nothing that needs numpy/scikit-learn for
    a fit this simple. Shared by GET /api/orders/forecast (daily) and
    this detector (hourly).
    """
    n = len(y_values)
    x_values = range(n)
    x_mean = sum(x_values) / n
    y_mean = sum(y_values) / n
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
    denominator = sum((x - x_mean) ** 2 for x in x_values)
    slope = numerator / denominator if denominator != 0 else 0.0
    intercept = y_mean - slope * x_mean
    return intercept, slope


@dataclass(frozen=True)
class ForecastVerdict:
    flagged: bool
    expected: float
    actual: float


def compute_forecast_deviation(
    history: list[float], actual: float, *, threshold: float = DEVIATION_THRESHOLD
) -> ForecastVerdict | None:
    """Pure function: fits a linear trend on `history` (hourly order
    counts, oldest first, zero-filled), projects the next point, and
    compares it to `actual`. None = no verdict possible (too little
    history), mirroring compute_zscore's "no verdict" case.
    """
    if len(history) < MIN_TRAINING_HOURS:
        return None

    intercept, slope = fit_linear_trend(history)
    # Floored at 0, same as the forecast endpoint: a steep downward trend
    # extrapolated one step can dip below zero, and a negative expected
    # order count isn't meaningful.
    expected = max(0.0, intercept + slope * len(history))
    deviation = abs(actual - expected) / max(expected, MIN_EXPECTED_ORDERS)
    return ForecastVerdict(flagged=deviation > threshold, expected=expected, actual=float(actual))


def score_buckets(
    buckets: list[HourlyBucket], *, recent_from: datetime, threshold: float = DEVIATION_THRESHOLD
) -> dict[tuple[str, str, datetime], ForecastVerdict]:
    """A verdict for each bucket whose hour is >= `recent_from` and that
    has at least MIN_TRAINING_HOURS of its own series' history before it.

    History for a bucket is that (region, brand) series' hourly counts
    from its first order in the window up to (not including) the bucket
    itself — the bucket being judged never trains the trend it's judged
    against. Hours inside that span with no orders count as 0 (zero-
    filled), same reasoning as zscore.score_order_volume: skipping quiet
    hours would overstate the series' typical volume.
    """
    counts_by_series: dict[tuple[str, str], dict[datetime, int]] = defaultdict(dict)
    for bucket in buckets:
        counts_by_series[(bucket.region, bucket.brand)][bucket.hour] = bucket.order_count

    verdicts: dict[tuple[str, str, datetime], ForecastVerdict] = {}
    for (region, brand), counts in counts_by_series.items():
        first_hour = min(counts)
        for hour, actual in counts.items():
            if hour < recent_from:
                continue
            hours_of_history = int((hour - first_hour) / _HOUR)
            history = [counts.get(first_hour + i * _HOUR, 0) for i in range(hours_of_history)]
            verdict = compute_forecast_deviation(history, actual, threshold=threshold)
            if verdict is not None:
                verdicts[(region, brand, hour)] = verdict
    return verdicts
