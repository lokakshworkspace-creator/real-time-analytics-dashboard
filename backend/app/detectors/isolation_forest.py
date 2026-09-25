"""Isolation Forest detector — multivariate, run as a periodic BATCH pass
(POST /api/detectors/run-batch), never per order.

Why batch, not per-order like detectors/zscore.py: an Isolation Forest
has no closed form to update one point at a time. It has to be *fitted*
on a whole window of data first (it builds random trees, then measures
how few splits it takes to isolate each point), and only then can it
score anything. Fitting on every POST /api/orders would mean rebuilding
a 100-tree forest per write, inside the ingest request, for a verdict
about a bucket that isn't even complete yet. The z-score detector is the
opposite shape — a mean and a stdev over a short window are cheap
enough to recompute on every write, which is exactly why detection-on-
write works for it (see zscore.py's module docstring). So the split is
deliberate: cheap univariate verdict at ingest, expensive multivariate
verdict in a batch.

What it adds over z-score: z-score sees one number (orders this hour).
This sees three at once — order_count, revenue, avg_order_value — so it
can flag a bucket whose *combination* is unusual even if each number
alone looks ordinary (e.g. normal order count, but every order is 10x
the usual value). Tree-based, so it needs no feature scaling: splits are
per-feature, so revenue being in the thousands and order_count in the
single digits doesn't let one feature dominate the way it would for a
distance-based method.

Known limitation, said plainly: one forest is fitted across ALL
(region, brand) buckets pooled together, so a naturally busy series is
judged against quiet ones. Fitting one forest per series would fix that
but needs far more history per series than a 7-day window of a
simulated catalog provides. It's the honest next step, not a claim that
pooling is ideal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sklearn.ensemble import IsolationForest

from .hourly_buckets import HourlyBucket

# Fewer buckets than this and a fitted forest is fitting noise — same
# cold-start reasoning (and same number) as zscore.MIN_WINDOW_SIZE. Below
# it the batch simply produces no Isolation Forest verdicts, leaving the
# fields null rather than guessing.
MIN_BUCKETS = 10

N_ESTIMATORS = 100

# "auto" uses the original paper's fixed offset (anomaly score > 0.5
# means "isolated faster than average") instead of asserting what
# fraction of buckets ought to be anomalous. A hard-coded contamination
# (say 0.05) would flag ~5% of buckets *by construction* even in a
# perfectly clean window; "auto" doesn't.
CONTAMINATION = "auto"

# A bucket is flagged only if its score EXCEEDS this by more than
# SCORE_TIE_TOLERANCE. The score is measured against 0.5 (the paper's
# "isolated about as fast as average" line), and we test it directly
# rather than trusting sklearn's predict() label, because of a real
# failure found by a test: given a perfectly constant window (every
# bucket identical, e.g. exactly 3 orders at the same value every hour)
# no tree can split anything, every score comes out at exactly 0.5, and
# predict() labelled ALL of them outliers — every steady order listed
# as an anomaly. A score of 0.5 means "nothing is more isolated than
# anything else", i.e. no outlier exists; it is the Isolation Forest's
# version of zscore.compute_zscore's stdev == 0 guard.
SCORE_THRESHOLD = 0.5
SCORE_TIE_TOLERANCE = 1e-6

# Fixed seed: the forest is random, and an anomaly verdict that changes
# between two runs over identical data would be impossible to demo,
# test, or defend.
RANDOM_STATE = 42


@dataclass(frozen=True)
class IsolationForestVerdict:
    flagged: bool
    # 0..1, HIGHER = more anomalous. sklearn's score_samples() is the
    # opposite sign (more negative = more anomalous); it's negated here
    # so every verdict this app stores reads the same way — bigger is
    # worse — like |z|. ~0.5 is the "average" boundary; well above it is
    # clearly isolated.
    score: float


def score_buckets(
    buckets: list[HourlyBucket], *, recent_from: datetime
) -> dict[tuple[str, str, datetime], IsolationForestVerdict]:
    """Fits an Isolation Forest on every bucket in `buckets` (the whole
    trailing window), then returns a verdict for each bucket whose hour
    is >= `recent_from`.

    Fitted on the whole window, *including* the recent buckets being
    scored: that's how an unsupervised forest is normally used, and an
    outlier is by definition rare enough that including it doesn't
    meaningfully move the trees the way it would move a mean/stdev.

    Returns {} (no verdicts) when the window has fewer than MIN_BUCKETS
    buckets. Pure function: no I/O, no clock, deterministic for a given
    input via RANDOM_STATE.
    """
    if len(buckets) < MIN_BUCKETS:
        return {}

    features = [[b.order_count, b.revenue, b.avg_order_value] for b in buckets]
    model = IsolationForest(
        n_estimators=N_ESTIMATORS, contamination=CONTAMINATION, random_state=RANDOM_STATE
    )
    model.fit(features)

    # score_samples is negative-is-anomalous; negate so bigger = worse.
    anomaly_scores = [float(-s) for s in model.score_samples(features)]

    return {
        bucket.key: IsolationForestVerdict(
            flagged=score > SCORE_THRESHOLD + SCORE_TIE_TOLERANCE, score=score
        )
        for bucket, score in zip(buckets, anomaly_scores)
        if bucket.hour >= recent_from
    }
