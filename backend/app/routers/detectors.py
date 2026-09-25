"""POST /api/detectors/run-batch — manually triggers the batch anomaly
pass (Isolation Forest + forecast deviation) over a trailing window.

Manual trigger, not a background scheduler, on purpose: a scheduler
(APScheduler or similar) would add a long-lived background task to the
app's lifecycle — start/stop hooks, overlapping-run guards, behavior
under multiple workers — none of which makes the detectors any more
correct, and all of which makes them harder to demo and to test. An
endpoint runs the identical code path deterministically, on demand, and
a scheduler is a small addition on top of it later (call run_batch on
an interval), not a rewrite.

Admin-only (Depends(require_admin)) and deliberately NOT brand-scoped:
the batch fits one Isolation Forest across every brand's buckets and
stamps verdicts on every brand's orders. A business account triggering
it would be running a cross-brand computation, and would learn nothing
it can't already read (its own brand's verdicts) from the anomaly feed.
The verdicts it writes are still brand-scoped on every read path.
"""

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..database import get_database
from ..detectors.batch import DEFAULT_RECENT_HOURS, DEFAULT_WINDOW_DAYS, run_batch
from ..models import BatchRunSummary, UserOut
from ..security import require_admin

router = APIRouter(prefix="/api", tags=["detectors"])


@router.post("/detectors/run-batch", response_model=BatchRunSummary)
async def run_detector_batch(
    window_days: int = Query(
        default=DEFAULT_WINDOW_DAYS, ge=1, le=30, description="Trailing window the detectors fit on."
    ),
    recent_hours: int = Query(
        default=DEFAULT_RECENT_HOURS,
        ge=1,
        le=24 * 7,
        description="How many of the most recent hours get a verdict stamped.",
    ),
    current_user: UserOut = Depends(require_admin),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> BatchRunSummary:
    return await run_batch(db, window_days=window_days, recent_hours=recent_hours)
