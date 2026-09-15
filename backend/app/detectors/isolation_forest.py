"""Isolation Forest anomaly detector - Phase 5b stretch goal.

Deliberately NOT wired into POST /api/metrics and NEVER writes to the
`anomaly` field: CLAUDE.md is explicit that whichever detector is
"live" must stay unambiguous, and running two detectors against every
write - one of which silently overwrites the other's verdict - would
break exactly that. z-score (zscore.py) is the only detector in the
ingest path, full stop.

This module is only ever invoked on demand, against documents already
stored by the ingest path, to produce an offline side-by-side
comparison against the z-score verdicts already sitting on those same
documents. It's a comparison tool, not an alternate production path.

Run (from backend/, with the venv active):

    python -m app.detectors.isolation_forest [--minutes N] [--min-samples N]

Must be run with `-m`, not `python app/detectors/isolation_forest.py`
directly - this module uses the same package-relative import
(`from ..config import settings`) as the rest of app/, which only
resolves when Python runs it as part of the `app` package.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from sklearn.ensemble import IsolationForest

from ..config import settings

# Mirrors zscore.py's MIN_WINDOW_SIZE: below this many samples, a
# metric+source group doesn't have enough history for IsolationForest
# to build a meaningful forest either, so it's skipped rather than
# scored on too little evidence.
DEFAULT_MIN_SAMPLES = 10

# Deliberately NOT tuned to the simulator's actual known spike rate
# (roughly 1-in-30-to-50 events, i.e. ~2-3% - see simulator.py). A real
# deployment doesn't know its true anomaly rate in advance, and hand-
# tuning `contamination` to match a rate we happen to know from the
# simulator's own source code would be fitting the detector to the
# test data, not building something that generalizes. "auto" is
# scikit-learn's own data-driven default.
CONTAMINATION = "auto"


def score_group(values: list[float]) -> list[bool]:
    """Fits one IsolationForest on `values` and returns a per-value
    flagged/not-flagged verdict, same order as the input.

    Single feature - just the metric value itself - deliberately
    mirroring z-score's own single-variable comparison: the point of
    running both detectors is to see how two approaches answer the
    *same* question ("is this value unusual for this metric+source?"),
    not to give Isolation Forest extra information z-score never gets.

    random_state is fixed so two runs over the same data produce the
    same verdicts - useful for a comparison report that should be
    reproducible, not a live production model that needs to react to
    new data every run.
    """
    features = [[v] for v in values]
    model = IsolationForest(contamination=CONTAMINATION, random_state=42)
    predictions = model.fit_predict(features)  # -1 = outlier, 1 = inlier
    return [p == -1 for p in predictions]


async def run(minutes: int, min_samples: int) -> None:
    """Pulls the last `minutes` of documents, groups them by
    metric+source (the same scope z-score uses), scores each
    sufficiently-large group, and prints every event either detector
    flagged - labeled BOTH / IF only / z-score only - plus a summary.

    Opens its own short-lived Motor client rather than reusing the
    FastAPI app's: this is a standalone script, not a request handler,
    so there's no app lifespan to borrow a connection from.
    """
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.mongodb_db_name]
    try:
        window_start = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        cursor = db.metrics.find({"timestamp": {"$gte": window_start}}).sort("timestamp", 1)
        documents = [doc async for doc in cursor]
    finally:
        client.close()

    if not documents:
        print(f"No documents in the last {minutes} minute(s). Nothing to score.")
        return

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for doc in documents:
        groups[(doc["metric"], doc["source"])].append(doc)

    print(
        f"Scoring {len(documents)} documents across {len(groups)} metric+source groups "
        f"(last {minutes} min, min {min_samples} samples/group)\n"
    )

    total_scored = 0
    total_if_flagged = 0
    total_zscore_flagged = 0
    total_agree = 0

    for (metric, source), docs in sorted(groups.items()):
        if len(docs) < min_samples:
            print(f"[{metric:<15} {source:<10}] skipped - only {len(docs)} samples (< {min_samples})")
            continue

        values = [d["value"] for d in docs]
        if_flags = score_group(values)

        group_if_flagged = 0
        group_zscore_flagged = 0
        group_agree = 0

        for doc, is_if_anomaly in zip(docs, if_flags):
            total_scored += 1
            is_zscore_anomaly = bool(doc.get("anomaly", False))

            if is_if_anomaly:
                total_if_flagged += 1
                group_if_flagged += 1
            if is_zscore_anomaly:
                total_zscore_flagged += 1
                group_zscore_flagged += 1
            if is_if_anomaly and is_zscore_anomaly:
                total_agree += 1
                group_agree += 1

            if is_if_anomaly or is_zscore_anomaly:
                verdict = (
                    "BOTH flag"
                    if is_if_anomaly and is_zscore_anomaly
                    else "IF only  "
                    if is_if_anomaly
                    else "z-score only"
                )
                ts = doc["timestamp"].isoformat()
                z = doc.get("z_score")
                z_display = f"{z:.2f}" if z is not None else "None"
                print(
                    f"  {ts}Z  {metric:<15} {source:<10} value={doc['value']:>8}  "
                    f"z_score={z_display:<8} -> {verdict}"
                )

        print(
            f"[{metric:<15} {source:<10}] {len(docs)} samples: "
            f"IF flagged {group_if_flagged}, z-score flagged {group_zscore_flagged}, "
            f"agree {group_agree}\n"
        )

    print(f"Scored {total_scored} documents total.")
    print(f"  Isolation Forest flagged: {total_if_flagged}")
    print(f"  z-score flagged:          {total_zscore_flagged}")
    print(f"  both agree:               {total_agree}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--minutes", type=int, default=120, help="How far back to pull documents from (default: 120)"
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help=f"Minimum samples per metric+source group to score (default: {DEFAULT_MIN_SAMPLES})",
    )
    args = parser.parse_args()
    asyncio.run(run(args.minutes, args.min_samples))


if __name__ == "__main__":
    main()
