"""Pydantic v2 models for the e-commerce business analytics API.

This replaces the earlier generic system-metrics models (cpu_usage,
memory_usage, response_time, ...) entirely — this dashboard now tracks
order events and inventory, not synthetic server metrics. Same
In/Out-per-resource pattern as before: a client's request body and the
server's response are always distinct models, even when field sets
overlap, so the input contract and the response contract can evolve
independently.
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_serializer


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
    silently *omits* the microseconds field whenever it's exactly 0,
    producing "...06Z" one time and "...06.325000Z" the next, purely
    depending on the value. %f is always zero-padded to 6 digits
    regardless, so every response has the identical shape.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# --- Orders -----------------------------------------------------------

PaymentStatus = Literal["success", "failed", "refunded"]


class OrderIn(BaseModel):
    """Request body for POST /api/orders."""

    order_id: str = Field(..., min_length=1)
    timestamp: datetime | None = Field(
        default=None,
        description="UTC event time. Defaults to server receive time if omitted.",
    )
    product_id: str = Field(..., min_length=1)
    product_name: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., gt=0)
    region: str = Field(..., min_length=1)
    payment_status: PaymentStatus = "success"


class OrderOut(BaseModel):
    """Response body for a stored order document (POST/GET /api/orders).

    `total_value` is server-computed (quantity * unit_price) rather than
    trusted from the client — a client could send a mismatched value
    otherwise, and every downstream aggregation (KPIs, regional/product
    revenue) depends on this number being correct. `anomaly` reflects
    the region's order-volume z-score at ingest time (see
    detectors/zscore.py's score_order_volume) — it is never client-
    supplied.
    """

    id: str
    order_id: str
    timestamp: datetime
    product_id: str
    product_name: str
    category: str
    quantity: int
    unit_price: float
    total_value: float
    region: str
    payment_status: PaymentStatus
    anomaly: bool

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return _format_utc_z(dt)


def order_document_to_out(document: dict) -> OrderOut:
    """Converts a raw MongoDB document into the API response model.

    Motor returns plain dicts with a BSON ObjectId in `_id`, which has
    no default JSON encoding FastAPI/Pydantic can use. Rather than
    writing a custom Pydantic type for ObjectId, we convert it to a
    plain string at this one boundary.
    """
    return OrderOut(
        id=str(document["_id"]),
        order_id=document["order_id"],
        timestamp=document["timestamp"],
        product_id=document["product_id"],
        product_name=document["product_name"],
        category=document["category"],
        quantity=document["quantity"],
        unit_price=document["unit_price"],
        total_value=document["total_value"],
        region=document["region"],
        payment_status=document["payment_status"],
        anomaly=document.get("anomaly", False),
    )


class BusinessAnomalyEvent(BaseModel):
    """One entry in GET /api/anomalies/business.

    Its own model rather than a reuse of OrderOut, mirroring the earlier
    metrics API's AnomalyEvent/MetricOut split: distinct endpoint,
    distinct contract. `z_score` is the number the region-hour
    order-volume detector actually computed for the hour this order fell
    into (see detectors/zscore.py) — the point of an anomaly panel is
    "flagged, and here's by how much", not just "flagged".
    """

    id: str
    order_id: str
    timestamp: datetime
    product_id: str
    product_name: str
    region: str
    quantity: int
    total_value: float
    anomaly: bool
    z_score: float | None = Field(
        default=None,
        description="The region-hour order-volume z-score that triggered this flag.",
    )

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return _format_utc_z(dt)


def order_document_to_anomaly(document: dict) -> BusinessAnomalyEvent:
    return BusinessAnomalyEvent(
        id=str(document["_id"]),
        order_id=document["order_id"],
        timestamp=document["timestamp"],
        product_id=document["product_id"],
        product_name=document["product_name"],
        region=document["region"],
        quantity=document["quantity"],
        total_value=document["total_value"],
        anomaly=document.get("anomaly", False),
        z_score=document.get("z_score"),
    )


# --- KPIs / aggregations -----------------------------------------------


class OrderKpis(BaseModel):
    """Response body for GET /api/orders/kpis — headline numbers for the
    dashboard's KPI card row, computed via a MongoDB aggregation
    pipeline ($match -> $group), never Python-side.

    Unlike the old /metrics/stats endpoint, an empty window returns
    zeroed-out numbers (200) rather than 404: these four cards are meant
    to render unconditionally at the top of the dashboard, and a 404
    would put the whole KPI row into an error state on a cold-started
    demo before the first order has posted. A window with genuinely no
    orders is a legitimate (if boring) answer — "nothing happened" — not
    a missing one.
    """

    minutes: int
    total_orders: int
    revenue: float
    units_sold: int
    avg_order_value: float


class RegionStats(BaseModel):
    """One entry in GET /api/orders/regions."""

    region: str
    orders: int
    revenue: float


class ProductStats(BaseModel):
    """One entry in GET /api/orders/products."""

    product_id: str
    product_name: str
    units_sold: int
    revenue: float


# --- Inventory ----------------------------------------------------------


class InventorySeedIn(BaseModel):
    """Request body for POST /api/inventory/seed.

    Used by the simulator to establish a starting stock level per
    product+region before any orders are posted. Upserted (see
    routers/inventory.py) so re-running the simulator's seed step is
    idempotent rather than erroring on a duplicate.
    """

    product_id: str = Field(..., min_length=1)
    product_name: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    region: str = Field(..., min_length=1)
    current_stock: int = Field(..., ge=0)


class InventoryItem(BaseModel):
    """Response entry for GET /api/inventory — current stock state for
    one product+region, mutated in place by every order (see
    routers/orders.py's decrement-on-write), unlike the append-only
    `orders` event log.
    """

    product_id: str
    product_name: str
    category: str
    region: str
    current_stock: int
    last_updated: datetime

    @field_serializer("last_updated")
    def serialize_last_updated(self, dt: datetime) -> str:
        return _format_utc_z(dt)


def inventory_document_to_item(document: dict) -> InventoryItem:
    return InventoryItem(
        product_id=document["product_id"],
        product_name=document["product_name"],
        category=document["category"],
        region=document["region"],
        current_stock=document["current_stock"],
        last_updated=document["last_updated"],
    )


RiskLevel = Literal["HIGH", "MEDIUM", "LOW"]


class InventoryRiskItem(BaseModel):
    """One entry in GET /api/inventory/risk — a product+region's current
    stock against its recent order demand, classified into a risk tier
    a dashboard badge can render directly (HIGH=red, MEDIUM=yellow,
    LOW=green) rather than the frontend re-deriving the threshold logic.
    """

    product_id: str
    product_name: str
    region: str
    current_stock: int
    recent_demand: int
    risk: RiskLevel
