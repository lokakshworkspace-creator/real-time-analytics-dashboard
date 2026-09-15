"""z-score anomaly detector — the MVP detection strategy specified in
CLAUDE.md.

Called synchronously inside POST /api/metrics, before the new event is
inserted (detection-on-write): it scores the incoming value against a
rolling window of the *prior* stored values for that exact metric+source
pair, and the caller stores the verdict with the document. Reads stay
cheap because nothing is computed at query time — the trade-off is that
changing this logic later requires reprocessing already-stored documents
to update their flags, since old verdicts don't retroactively change.

See detectors/isolation_forest.py for the deliberately separate,
never-automatic alternative detector.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from motor.motor_asyncio import AsyncIOMotorDatabase

# How many of the most recent metric+source events form the rolling
# baseline. Bounded — not "every event ever for this metric+source" —
# so the baseline reflects recent behavior rather than slowly going
# stale as weeks of history pile up behind it.
WINDOW_SIZE = 30

# Minimum prior points required before detection activates at all: the
# "cold start" problem CLAUDE.md calls out by name. A mean/stdev
# computed from 2 or 3 points isn't a trustworthy baseline — it's
# noise pretending to be a distribution — so below this threshold every
# event is anomaly=False rather than risking false positives on
# essentially no history.
MIN_WINDOW_SIZE = 10

# |z| beyond this is flagged. Per CLAUDE.md: under a normal
# distribution, ~99.7% of values fall within 3 standard deviations of
# the mean, so a breach is a genuinely rare event, not routine noise.
Z_THRESHOLD = 3.0


@dataclass(frozen=True)
class ZScoreResult:
    is_anomaly: bool
    z_score: float | None  # None = no verdict was possible (cold start, or a constant window)
    window_size: int  # how many prior points the verdict (or non-verdict) is based on


async def score(
    db: AsyncIOMotorDatabase, *, metric: str, source: str, value: float
) -> ZScoreResult:
    """Scores `value` against metric+source history already in MongoDB.

    Must be called BEFORE the new event is inserted — the window is
    built entirely from what's already stored, so it never includes the
    point currently being scored. Uses `metric_1_timestamp_-1`'s metric
    equality bound to narrow the scan; there's no metric+source index
    (not requested for this phase — see README's Phase 5 design notes
    for the trade-off), so the `source` filter is applied as a fetch
    filter across that metric's documents rather than via its own index
    range, which is fine at this project's data volume.
    """
    cursor = db.metrics.find(
        {"metric": metric, "source": source},
        projection={"value": 1, "_id": 0},
        sort=[("timestamp", -1)],
        limit=WINDOW_SIZE,
    )
    window = [doc["value"] async for doc in cursor]

    if len(window) < MIN_WINDOW_SIZE:
        return ZScoreResult(is_anomaly=False, z_score=None, window_size=len(window))

    mean = statistics.mean(window)
    # Sample stdev (N-1 denominator): window is a sample of this
    # metric+source's behavior, not its entire population. Well-defined
    # here since len(window) >= MIN_WINDOW_SIZE >= 10, i.e. N-1 >= 9.
    stdev = statistics.stdev(window)

    if stdev == 0:
        # A perfectly constant recent window (e.g. failed_requests has
        # sat at exactly 2 for the last 10 events). Any deviation from
        # a zero-spread window is technically an infinite z-score,
        # which isn't a meaningful signal to act on — treat it as "no
        # verdict possible", not as an automatic flag.
        return ZScoreResult(is_anomaly=False, z_score=None, window_size=len(window))

    z = (value - mean) / stdev
    return ZScoreResult(is_anomaly=abs(z) > Z_THRESHOLD, z_score=z, window_size=len(window))
