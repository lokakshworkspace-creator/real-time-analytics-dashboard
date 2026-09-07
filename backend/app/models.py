"""Pydantic v2 models for the metrics API.

Two models instead of one: MetricIn is what a client is allowed to send
(no `anomaly` field — clients don't get to self-report that), MetricOut
is what the API returns (includes server-computed fields). Keeping them
separate means the input validation surface and the response contract
can evolve independently, e.g. Phase 5 will change how `anomaly` gets
set without touching what clients are allowed to POST.
"""

from datetime import datetime, timezone
from typing import Literal

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


class MetricIn(BaseModel):
    """Request body for POST /api/metrics."""

    metric: MetricName
    value: float
    source: str = Field(..., min_length=1, description="Emitting host/service, e.g. 'server-2'.")
    timestamp: datetime | None = Field(
        default=None,
        description="UTC event time. Defaults to server receive time if omitted.",
    )


class MetricOut(BaseModel):
    """Response body for a stored metric document."""

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
        """Always emit UTC ISO-8601 with an explicit 'Z', matching the
        data model in CLAUDE.md (e.g. "2026-08-30T10:31:06Z").

        Motor/PyMongo store BSON dates as UTC but hand them back as
        *naive* datetimes (no tzinfo) — Pydantic's default datetime
        serialization would then omit any offset, which is ambiguous
        for API consumers. We treat a naive datetime as UTC (the only
        thing it can be, given where it came from) and convert an
        aware one to UTC, so the output format is identical either way.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")


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
