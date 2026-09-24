"""Shared CSV-export helper for GET /api/orders, GET /api/orders/products,
and GET /api/inventory/risk's `format=csv` branch.

A query param (`?format=csv`) rather than a separate `/export` route or
suffix: each of these three endpoints already has its own filtering/
pagination query params (limit, minutes, order, ...), and `format=csv`
reuses that exact same filtered result set instead of re-implementing
the same query twice under two different paths that could drift out of
sync with each other. FastAPI only applies a route's `response_model`
to a plain returned value — a Response subclass (StreamingResponse,
here) returned directly from the handler is sent as-is, so the JSON and
CSV branches can share one route/one response_model declaration without
the response_model getting in the CSV branch's way (see each router's
`response_model=None` routes for where this matters for OpenAPI).
"""

import csv
import io

from starlette.responses import StreamingResponse


def csv_streaming_response(
    rows: list[dict], *, fieldnames: list[str], filename: str
) -> StreamingResponse:
    """Serializes `rows` (already-formatted dicts, e.g. from a Pydantic
    model's .model_dump()) as CSV and returns them as a downloadable
    attachment.

    Built in memory via io.StringIO rather than a true generator/cursor
    stream: every result set this app ever returns is already fully
    materialized in Python before this is called (the same aggregation
    results the JSON branch would return), so there's no large query
    result to stream incrementally — StreamingResponse is used here for
    the response *type* FastAPI expects for a non-JSON body, not because
    the data itself needs chunked delivery.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
