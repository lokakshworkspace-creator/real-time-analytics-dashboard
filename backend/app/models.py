"""Pydantic v2 models for the metrics API.

Two models instead of one: MetricIn is what a client is allowed to send
(no `anomaly` field — clients don't get to self-report that), MetricOut
is what the API returns (includes server-computed fields). Keeping them
separate means the input validation surface and the response contract
can evolve independently, e.g. Phase 5 will change how `anomaly` gets
set without touching what clients are allowed to POST.
"""

from datetime import datetime, timezone
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_serializer

# The five metrics this MVP simulates and displays (see CLAUDE.md). A
# Literal instead of a bare str catches typos/garbage metric names at
# the API boundary instead of letting them silently pollute the
# collection. Trade-off: adding a new metric means editing this type,
# which is an intentional, small bit of friction rather than silent
# schema drift.
MetricName = Literal[
    "orders",
    "response_time",
    "cpu_usage",
    "failed_requests",
    "memory_usage",
]

# Single source of truth for "all 5 metric names" as a plain iterable —
# used by GET /metrics/latest (Phase 4) to know what to look up without
# duplicating this list a second time and risking it drifting out of
# sync with the Literal above.
METRIC_NAMES: tuple[str, ...] = get_args(MetricName)


class MetricIn(BaseModel):
    """Request body for POST /api/metrics."""

    metric: MetricName
    # allow_inf_nan=False (Phase 8 hardening): Python's json module — and
    # so Pydantic's default float parsing — accepts the non-standard
    # tokens NaN/Infinity/-Infinity, which plain float() happily returns
    # as real IEEE-754 values. Without this constraint those values
    # would sail through validation and land in a metric+source's
    # rolling window (detectors/zscore.py), silently poisoning every
    # z-score computed from that window for the next WINDOW_SIZE events
    # — NaN propagates through mean/stdev, and `abs(nan) > 3` is always
    # False in Python, so a NaN value could never even be flagged as the
    # anomaly it obviously is. Rejecting it at the API boundary (422) is
    # far cheaper than reasoning about a poisoned rolling window later.
    value: float = Field(..., allow_inf_nan=False)
    source: str = Field(..., min_length=1, description="Emitting host/service, e.g. 'server-2'.")
    timestamp: datetime | None = Field(
        default=None,
        description="UTC event time. Defaults to server receive time if omitted.",
    )


def _format_utc_z(dt: datetime) -> str:
    """Always renders UTC ISO-8601 with an explicit 'Z' AND a fixed-width
    6-digit fractional-seconds field, e.g. "2026-08-30T10:31:06.000000Z".

    Motor/PyMongo store BSON dates as UTC but hand them back as *naive*
    datetimes (no tzinfo) — Pydantic's default datetime serialization
    would then omit any offset, which is ambiguous for API consumers.
    We treat a naive datetime as UTC (the only thing it can be, given
    where it came from) and convert an aware one to UTC, so the output
    format is identical either way. Shared by every response model
    below instead of repeating this logic per model.

    Explicit strftime("...%f") instead of dt.isoformat(): isoformat()
    silently *omits* the microseconds field whenever it's exactly 0
    (a value landing on a whole second, e.g. no explicit timestamp was
    given and datetime.now() happened to round cleanly, or a
    hand-constructed test timestamp) — producing "...06Z" one time and
    "...06.325000Z" the next, purely depending on the value, not a
    format decision. %f is always zero-padded to 6 digits regardless,
    so every response has the identical shape.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class MetricOut(BaseModel):
    """Response body for a stored metric document (POST /api/metrics)."""

    id: str
    timestamp: datetime
    metric: str
    value: float
    source: str
    anomaly: bool = Field(
        default=False,
        description="Always False for now — z-score detection is wired in during Phase 5.",
    )

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return _format_utc_z(dt)


def metric_document_to_out(document: dict) -> MetricOut:
    """Converts a raw MongoDB document into the API response model.

    Motor returns plain dicts with a BSON ObjectId in `_id`, which has
    no default JSON encoding FastAPI/Pydantic can use. Rather than
    writing a custom Pydantic type for ObjectId, we convert it to a
    plain string at this one boundary — simpler to read and explain,
    and it's the only place in the codebase that needs to know Mongo
    stores ids as ObjectId at all.
    """
    return MetricOut(
        id=str(document["_id"]),
        timestamp=document["timestamp"],
        metric=document["metric"],
        value=document["value"],
        source=document["source"],
        anomaly=document.get("anomaly", False),
    )


class LatestMetric(BaseModel):
    """One entry in the response of GET /metrics/latest.

    Structurally similar to MetricOut, but kept as its own model rather
    than reused: this endpoint's contract (one snapshot per metric,
    always the newest) is conceptually different from "the document
    POST /api/metrics just created", and giving it its own name keeps
    the OpenAPI docs and any future frontend types honest about which
    endpoint they came from, even though today the fields match.
    """

    id: str
    metric: str
    value: float
    source: str
    timestamp: datetime
    anomaly: bool

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return _format_utc_z(dt)


def metric_document_to_latest(document: dict) -> LatestMetric:
    return LatestMetric(
        id=str(document["_id"]),
        metric=document["metric"],
        value=document["value"],
        source=document["source"],
        timestamp=document["timestamp"],
        anomaly=document.get("anomaly", False),
    )


class MetricStats(BaseModel):
    """Response body for GET /metrics/stats — aggregate numbers only,
    computed in MongoDB (see routers/analytics.py), never raw documents.
    """

    metric: str
    minutes: int = Field(..., description="Size of the trailing window these stats cover.")
    count: int
    avg: float
    min: float
    max: float


class AnomalyEvent(BaseModel):
    """One entry in GET /metrics/anomalies (Phase 5).

    Its own model, not a reuse of MetricOut/LatestMetric, for the same
    reason those two are separate from each other: distinct endpoint,
    distinct contract. This one also carries `z_score` — the number the
    z-score detector actually computed for this event (see
    detectors/zscore.py) — which is the whole point of an anomaly
    panel: not just "flagged", but "flagged, and here's by how much".
    """

    id: str
    metric: str
    value: float
    source: str
    timestamp: datetime
    anomaly: bool
    z_score: float | None = Field(
        default=None,
        description="The z-score that triggered this flag. None if the document predates "
        "Phase 5 or was flagged when a verdict wasn't possible (shouldn't normally happen "
        "for anomaly=True, but the field stays optional rather than assumed).",
    )

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return _format_utc_z(dt)


def metric_document_to_anomaly(document: dict) -> AnomalyEvent:
    return AnomalyEvent(
        id=str(document["_id"]),
        metric=document["metric"],
        value=document["value"],
        source=document["source"],
        timestamp=document["timestamp"],
        anomaly=document.get("anomaly", False),
        z_score=document.get("z_score"),
    )


class HistoryPoint(BaseModel):
    """One entry in GET /metrics/history (Phase 6).

    Deliberately minimal — just what a trend chart needs to plot a
    point — not a reuse of MetricOut/AnomalyEvent, which carry an `id`
    and other fields no chart axis needs. This endpoint was added
    specifically so the frontend can render real history on mount with
    one fetch, without needing the client-side polling loop that's
    reserved for Phase 7.
    """

    timestamp: datetime
    value: float

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return _format_utc_z(dt)


def metric_document_to_history_point(document: dict) -> HistoryPoint:
    return HistoryPoint(timestamp=document["timestamp"], value=document["value"])
