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
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
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

# Kept here (not detectors/zscore.py) alongside PaymentStatus/RiskLevel:
# this file is the single source of truth for every Literal type shared
# between a stored document's shape and an API response model. The
# detector still owns the actual boundary values (see zscore.py's
# _severity_for) — this is just the type name they map into.
AnomalySeverity = Literal["mild", "moderate", "severe"]


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
    brand: str = Field(..., min_length=1, description="e.g. 'Nike'. Drives business-account scoping.")
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
    brand: str
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
        brand=document["brand"],
        quantity=document["quantity"],
        unit_price=document["unit_price"],
        total_value=document["total_value"],
        region=document["region"],
        payment_status=document["payment_status"],
        anomaly=document.get("anomaly", False),
    )


class ZScoreVerdict(BaseModel):
    """The z-score detector's verdict on an order (detectors/zscore.py),
    scored at ingest. Always present — every order has one. `score` is
    None when no verdict was possible (cold start, or a constant window).
    """

    flagged: bool
    score: float | None = None
    severity: AnomalySeverity | None = Field(
        default=None,
        description="Tier derived from |score| — see detectors/zscore.py. None unless flagged.",
    )


class IsolationForestVerdictOut(BaseModel):
    """The Isolation Forest's verdict on the order's (region, brand, hour)
    bucket (detectors/isolation_forest.py). `score` is 0..1, higher =
    more anomalous.
    """

    flagged: bool
    score: float


class ForecastVerdictOut(BaseModel):
    """The forecast-deviation verdict on the order's (region, brand,
    hour) bucket (detectors/forecast_deviation.py): what the fitted
    trend expected vs. the actual order count that hour.
    """

    flagged: bool
    expected: float
    actual: float


def compute_detector_agreement(
    z_flagged: bool, isolation_forest_flagged: bool | None, forecast_flagged: bool | None
) -> int:
    """How many of the three detectors flagged this record, 0-3. A
    detector that hasn't scored it (None — predates the batch job, or
    too little history) counts as not having flagged it: absence of a
    verdict is not agreement.
    """
    return int(bool(z_flagged)) + int(bool(isolation_forest_flagged)) + int(bool(forecast_flagged))


class BusinessAnomalyEvent(BaseModel):
    """One entry in GET /api/anomalies/business — one order record with
    all three detectors' verdicts side by side.

    Its own model rather than a reuse of OrderOut, mirroring the earlier
    metrics API's AnomalyEvent/MetricOut split: distinct endpoint,
    distinct contract. A record appears here if ANY detector flagged it,
    so `z_score.flagged` can be false on a row an Isolation Forest or
    forecast flag brought in. `isolation_forest` / `forecast` are null
    for records no batch run has scored (see detectors/batch.py), which
    is different from a verdict of flagged=false. `detector_agreement`
    is how many detectors flagged it (0-3) — several independent
    methods agreeing is stronger evidence than one, and is what
    GET /api/alerts uses to rank.
    """

    id: str
    order_id: str
    timestamp: datetime
    product_id: str
    product_name: str
    brand: str
    region: str
    quantity: int
    total_value: float
    z_score: ZScoreVerdict
    isolation_forest: IsolationForestVerdictOut | None = None
    forecast: ForecastVerdictOut | None = None
    detector_agreement: int = Field(ge=0, le=3)

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return _format_utc_z(dt)


def order_document_to_anomaly(document: dict) -> BusinessAnomalyEvent:
    z_flagged = document.get("anomaly", False)
    isolation_forest = None
    if document.get("is_anomaly_if") is not None:
        isolation_forest = IsolationForestVerdictOut(
            flagged=document["is_anomaly_if"], score=document["if_score"]
        )
    forecast = None
    if document.get("is_anomaly_forecast") is not None:
        forecast = ForecastVerdictOut(
            flagged=document["is_anomaly_forecast"],
            expected=document["forecast_expected"],
            actual=document["forecast_actual"],
        )

    return BusinessAnomalyEvent(
        id=str(document["_id"]),
        order_id=document["order_id"],
        timestamp=document["timestamp"],
        product_id=document["product_id"],
        product_name=document["product_name"],
        brand=document["brand"],
        region=document["region"],
        quantity=document["quantity"],
        total_value=document["total_value"],
        z_score=ZScoreVerdict(
            flagged=z_flagged, score=document.get("z_score"), severity=document.get("severity")
        ),
        isolation_forest=isolation_forest,
        forecast=forecast,
        detector_agreement=compute_detector_agreement(
            z_flagged,
            isolation_forest.flagged if isolation_forest else None,
            forecast.flagged if forecast else None,
        ),
    )


# --- KPIs / aggregations -----------------------------------------------


class KpiValues(BaseModel):
    """The four headline numbers for one period — shared shape between
    OrderKpis's `current` and `previous`, so a frontend can render both
    with the same formatting code.
    """

    total_orders: int
    revenue: float
    units_sold: int
    avg_order_value: float


class KpiChangePct(BaseModel):
    """Percent change, current vs. previous, per KpiValues field.

    Each field is `None` rather than 0 or an error when the *previous*
    period had zero orders — "up from zero" has no defined percentage,
    and returning `null` lets the frontend render "New" or "—" instead
    of a nonsensical 0% or an infinite/undefined number.
    """

    total_orders: float | None
    revenue: float | None
    units_sold: float | None
    avg_order_value: float | None


class OrderKpis(BaseModel):
    """Response body for GET /api/orders/kpis — headline numbers for the
    dashboard's KPI card row, PLUS a period-over-period comparison,
    computed via a MongoDB aggregation pipeline ($match -> $facet ->
    $group per branch), never Python-side except the change_pct math
    itself (see routers/orders.py — dividing by a possibly-zero
    previous value is simpler to guard in plain Python than inside an
    aggregation expression).

    `current`/`previous` always return zeroed-out numbers (never a
    missing period) for a window with no orders — these cards are meant
    to render unconditionally at the top of the dashboard, and a 404
    would put the whole KPI row into an error state on a cold-started
    demo before the first order has posted. A window with genuinely no
    orders is a legitimate (if boring) answer — "nothing happened" —
    not a missing one.
    """

    range: str
    current: KpiValues
    previous: KpiValues
    change_pct: KpiChangePct


class RegionPeriodValues(BaseModel):
    """orders/revenue for one region over one period — the `current`/
    `previous` shape inside RegionStats, mirroring KpiValues.
    """

    orders: int
    revenue: float


class RegionChangePct(BaseModel):
    """Percent change, current vs. previous, per RegionPeriodValues
    field — same null-on-zero-previous convention as KpiChangePct.
    """

    orders: float | None
    revenue: float | None


class RegionStats(BaseModel):
    """One entry in GET /api/orders/regions — a region's orders/revenue
    for the current `range`, the immediately preceding period of equal
    length, and the percent change between them. Same current/previous/
    change_pct shape as OrderKpis, reused rather than reinvented (see
    routers/orders.py's compute_change_pct) — just grouped per-region
    instead of collapsed into one overall total.
    """

    region: str
    current: RegionPeriodValues
    previous: RegionPeriodValues
    change_pct: RegionChangePct


class ProductPeriodValues(BaseModel):
    """units_sold/revenue for one product over one period."""

    units_sold: int
    revenue: float


class ProductChangePct(BaseModel):
    """Percent change, current vs. previous, per ProductPeriodValues field."""

    units_sold: float | None
    revenue: float | None


class ProductStats(BaseModel):
    """One entry in GET /api/orders/products — same current/previous/
    change_pct shape as RegionStats/OrderKpis, see their docstrings.
    """

    product_id: str
    product_name: str
    brand: str
    current: ProductPeriodValues
    previous: ProductPeriodValues
    change_pct: ProductChangePct


class ProductStatsResponse(BaseModel):
    """Response body for GET /api/orders/products.

    A wrapper around `products` (rather than a bare list, as this
    endpoint used to return) specifically to carry `note`: when the
    catalog has too few distinct products for a non-overlapping
    top-N/bottom-N split (e.g. 5 products total, limit=5 requested for
    both directions), the endpoint caps each direction to a
    non-overlapping half rather than silently returning identical or
    overlapping lists under `order=top` and `order=bottom` — see
    routers/orders.py's get_product_stats for the exact split logic.
    `note` is populated only when that capping actually happened;
    otherwise it's `null` and every request behaves as it always did.
    """

    note: str | None = Field(
        default=None,
        description="Non-null only when the catalog was too small for the requested limit "
        "in this direction — explains the cap, see get_product_stats.",
    )
    products: list[ProductStats]


class ForecastPoint(BaseModel):
    """One projected future day in GET /api/orders/forecast."""

    date: str
    projected_orders: float
    projected_revenue: float


class ForecastResponse(BaseModel):
    """Response body for GET /api/orders/forecast — see
    routers/orders.py's get_order_forecast for the exact method.
    `method` names the projection technique honestly (e.g.
    "linear_regression_last_42_days" or, for a product with too little
    history to fit a trend, "insufficient_history_flat_projection") so
    a consumer never mistakes this for something more sophisticated
    than a simple, explainable regression over recent daily buckets.
    """

    product_id: str
    method: str
    forecast: list[ForecastPoint]


class BrandBenchmark(BaseModel):
    """One brand's entry in GET /api/brands/benchmark — its own
    orders/revenue/avg_order_value for the window, plus how its revenue
    compares to the average revenue across every brand in the same
    window. `revenue_vs_average_pct` reuses the exact same percent-
    change math as every other change_pct field in this app
    (routers/orders.py's compute_change_pct) — "how far above/below
    average" is the same divide-by-a-possibly-zero-baseline question as
    "how far above/below last period," just with a different baseline.
    """

    brand: str
    orders: int
    revenue: float
    avg_order_value: float
    revenue_vs_average_pct: float | None = Field(
        default=None,
        description="None only when there's no cross-brand average to compare against "
        "(e.g. every brand has zero revenue in this window).",
    )


class BrandBenchmarkResponse(BaseModel):
    """Response body for GET /api/brands/benchmark — admin-only (see
    security.require_admin). `average_revenue` is the plain arithmetic
    mean of each listed brand's own revenue (not weighted by order
    count), the same baseline every `revenue_vs_average_pct` above is
    measured against.
    """

    range: str
    average_revenue: float
    benchmarks: list[BrandBenchmark]


class TrendPoint(BaseModel):
    """One entry in GET /api/orders/trend — orders/revenue/units_sold
    bucketed by day/week/month, oldest first. `period` is the bucket's
    start date (UTC), formatted "YYYY-MM-DD" regardless of granularity
    — a week or month bucket is still identified by the date it starts
    on, which is enough for a chart x-axis without needing a second
    "granularity" field on every point.
    """

    period: str
    orders: int
    revenue: float
    units_sold: int


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
    brand: str = Field(..., min_length=1)
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
    brand: str
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
        brand=document["brand"],
        region=document["region"],
        current_stock=document["current_stock"],
        last_updated=document["last_updated"],
    )


RiskLevel = Literal["HIGH", "MEDIUM", "LOW"]


class ReorderSuggestion(BaseModel):
    """How much to reorder, and by when — see routers/inventory.py's
    get_inventory_risk for the exact math and the named constants
    (LEAD_TIME_BUFFER_DAYS, REORDER_URGENCY_THRESHOLD_DAYS) behind it.
    Only present when `daily_demand_rate` is nonzero — there's nothing
    defensible to suggest for a product with no recent sales signal.
    """

    suggested_quantity: int = Field(
        ..., ge=0, description="Units to order to cover LEAD_TIME_BUFFER_DAYS of projected demand."
    )
    suggested_by_date: str = Field(
        ..., description="YYYY-MM-DD — when stock would cross REORDER_URGENCY_THRESHOLD_DAYS of runway."
    )


class InventoryRiskItem(BaseModel):
    """One entry in GET /api/inventory/risk — a product+region's current
    stock against its recent order demand, classified into a risk tier
    a dashboard badge can render directly (HIGH=red, MEDIUM=yellow,
    LOW=green) rather than the frontend re-deriving the threshold logic.
    """

    product_id: str
    product_name: str
    brand: str
    region: str
    current_stock: int
    recent_demand: int
    risk: RiskLevel
    daily_demand_rate: float = Field(
        ..., description="recent_demand divided by the lookback window's length in days."
    )
    days_of_stock_remaining: float | None = Field(
        default=None,
        description="current_stock / daily_demand_rate. None when daily_demand_rate is 0 — "
        "can't estimate depletion for a product with no recent sales.",
    )
    reorder_suggestion: ReorderSuggestion | None = None


class ExplanationResponse(BaseModel):
    """Response body for POST /api/anomalies/{id}/explain. `cached` is
    true when this came straight from the anomaly record (no model call
    was made), false when it was generated by this request. The same
    text is stored either way, so a second call is always free.
    """

    anomaly_id: str
    explanation: str
    suggested_action: str
    explained_at: datetime
    cached: bool

    @field_serializer("explained_at")
    def serialize_explained_at(self, dt: datetime) -> str:
        return _format_utc_z(dt)


class BatchRunSummary(BaseModel):
    """Response body for POST /api/detectors/run-batch — what one batch
    pass over the trailing window actually did (see detectors/batch.py).
    `*_buckets_scored` counts buckets that got a verdict (flagged or
    not); a detector with too little history for a bucket scores fewer
    than the window holds, so scored < buckets_in_window is normal, not
    an error.
    """

    window_days: int
    recent_hours: int
    buckets_in_window: int
    isolation_forest_buckets_scored: int
    isolation_forest_buckets_flagged: int
    forecast_buckets_scored: int
    forecast_buckets_flagged: int
    orders_stamped: int
    note: str | None = None


AlertType = Literal["anomaly", "low_stock", "decline"]


class Alert(BaseModel):
    """One entry in GET /api/alerts — a single consolidated feed over
    three otherwise-separate signals (see routers/alerts.py):
    business anomalies, HIGH-risk inventory items, and a region/product
    whose revenue declined beyond a configurable threshold. `severity`
    reuses the same mild/moderate/severe vocabulary as anomaly
    detection (AnomalySeverity) across all three types, specifically so
    the feed can be sorted and displayed uniformly rather than needing
    a different severity scale per alert type. `related_entity` is
    deliberately a loose dict, not a strict per-type model — what it
    identifies (a region, a product+region, an order) genuinely differs
    by `type`, and forcing one shape on all three would mean mostly-
    null fields either way.
    """

    type: AlertType
    severity: AnomalySeverity
    message: str
    timestamp: datetime
    related_entity: dict[str, str | None]
    # Only set on type="anomaly": which of "z_score" / "isolation_forest"
    # / "forecast" flagged it (drives the frontend's detector badges),
    # and how many — the same count as BusinessAnomalyEvent.detector_agreement.
    # Null on the other alert types, which have no notion of detectors.
    detectors: list[str] | None = None
    detector_agreement: int | None = None

    @field_serializer("timestamp")
    def serialize_timestamp(self, dt: datetime) -> str:
        return _format_utc_z(dt)


# --- Users / auth --------------------------------------------------------

UserRole = Literal["admin", "business"]

# One definition of "a valid new password", shared by registration and
# password change so the two can never drift apart. 72 is bcrypt's own
# hard input limit (see security.py) — rejected with a clear 422 rather
# than silently truncated.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 72


class UserIn(BaseModel):
    """Request body for POST /api/auth/register.

    `business_name`/`owned_brands` are only meaningful for role="business"
    — validated together below rather than as two independent optional
    fields, so a malformed business registration (missing brands, missing
    name) is rejected at the API boundary with a clear 422 instead of
    silently creating a business account that can never see any data.
    """

    email: EmailStr
    password: str = Field(
        ...,
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description="Hashed before storage, never stored raw. 72 bytes is bcrypt's own hard "
        "limit — rejected here with a clear 422 rather than silently truncated.",
    )
    role: UserRole
    business_name: str | None = Field(default=None, description="Required if role='business'.")
    owned_brands: list[str] = Field(
        default_factory=list,
        description="Brands this account can see, e.g. ['Nike']. Required (non-empty) if role='business'.",
    )

    @model_validator(mode="after")
    def _validate_business_fields(self) -> "UserIn":
        if self.role == "business":
            if not self.business_name:
                raise ValueError("business_name is required when role='business'")
            if not self.owned_brands:
                raise ValueError("owned_brands must be a non-empty list when role='business'")
        return self


class UserOut(BaseModel):
    """Response body for a stored user — never includes the password
    hash. Returned by register/login's underlying lookup and by
    GET /api/auth/me, and reused as the type `get_current_user`
    resolves to (see security.py) so every router that scopes a query
    by role/owned_brands works with one consistent shape.
    """

    user_id: str
    email: str
    role: UserRole
    business_name: str | None = None
    owned_brands: list[str] = Field(default_factory=list)


def user_document_to_out(document: dict) -> UserOut:
    return UserOut(
        user_id=str(document["_id"]),
        email=document["email"],
        role=document["role"],
        business_name=document.get("business_name"),
        owned_brands=document.get("owned_brands", []),
    )


class LoginIn(BaseModel):
    """Request body for POST /api/auth/login."""

    email: EmailStr
    password: str = Field(..., min_length=1)


class ProfileUpdateIn(BaseModel):
    """Request body for PATCH /api/auth/me — the ONE thing a user may
    change about their own account: a business account's display name.

    `extra="forbid"` is the guard that matters: role, owned_brands,
    email and everything else are authorization- or identity-defining
    fields that only an admin sets at registration, so a body naming
    any of them is rejected outright (a 422 naming the field) rather
    than silently ignored — an ignored field would read to a client as
    "saved", and would leave a real privilege-escalation attempt (say,
    adding a brand to owned_brands) looking like it half-worked.
    Whitespace is stripped and a blank name is rejected.
    """

    model_config = ConfigDict(extra="forbid")

    business_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class PasswordChangeIn(BaseModel):
    """Request body for POST /api/auth/change-password.

    `new_password` follows exactly the registration rules (see
    PASSWORD_MIN_LENGTH / PASSWORD_MAX_LENGTH). `current_password` is
    only checked for length so an over-long value gets a 422 instead of
    reaching bcrypt (which rejects >72 bytes with an error, not a
    verdict). A new password identical to the current one is rejected: a
    "change" that changes nothing is almost always a slip, and telling
    the user beats reporting a success that wasn't one.
    """

    current_password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)

    @model_validator(mode="after")
    def _new_differs_from_current(self) -> "PasswordChangeIn":
        if self.new_password == self.current_password:
            raise ValueError("new_password must be different from current_password")
        return self


class MessageOut(BaseModel):
    """A plain acknowledgement body — used where success has nothing to
    return and, specifically, must not echo anything sensitive back.
    """

    message: str


class TokenOut(BaseModel):
    """Response body for POST /api/auth/login."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Seconds until the token expires.")
