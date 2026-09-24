"""Synthetic order-event producer for the e-commerce analytics
dashboard.

Posts one order event per tick to POST /api/orders over plain HTTP -
this script never imports from `app/` or touches MongoDB directly. That's
deliberate: in the real architecture (see CLAUDE.md), an order producer
is just another client of the API, same as any other service would be.
Keeping the simulator an HTTP client instead of a DB writer means it
exercises the exact same validation/storage/detection path a real
producer would hit.

On startup, seeds a starting stock level for every product+region
combination via POST /api/inventory/seed before posting any orders -
otherwise every order's inventory decrement would silently no-op
against a product+region nothing has ever seeded (see
routers/orders.py's warning-and-continue behavior for that case).

Run:    python simulator.py [--url URL] [--interval SECONDS] [--duration SECONDS]
Stop:   Ctrl+C - the loop catches KeyboardInterrupt, closes its HTTP
        session, and prints a summary.
"""

from __future__ import annotations

import argparse
import os
import random
import time
import uuid
from dataclasses import dataclass

import requests

REGIONS = ["North America", "Europe", "Asia Pacific", "Latin America", "Middle East"]

# Relative likelihood of an order landing in each region - deliberately
# uneven so regional KPIs/charts have real differences to show, not five
# near-identical bars. Same length/order as REGIONS.
REGION_WEIGHTS = [0.35, 0.30, 0.20, 0.10, 0.05]


@dataclass(frozen=True)
class Product:
    product_id: str
    product_name: str
    category: str
    brand: str
    unit_price: float


# Brand-prefixed product names (added alongside role-based auth): each
# product now belongs to a real, recognizable brand so business
# accounts have something meaningful to be scoped to — see
# routers/auth.py and security.brand_match_stage. One brand per
# product, chosen to plausibly fit that product's category rather than
# assigned arbitrarily.
PRODUCTS = [
    Product("sku-001", "Nike Running Shoes", "Apparel", "Nike", 89.99),
    Product("sku-002", "Sony Wireless Earbuds", "Electronics", "Sony", 59.99),
    Product("sku-003", "Hydro Flask Water Bottle", "Home & Kitchen", "Hydro Flask", 24.99),
    Product("sku-004", "Logitech Mechanical Keyboard", "Electronics", "Logitech", 119.99),
    Product("sku-005", "Lululemon Yoga Mat", "Sporting Goods", "Lululemon", 34.99),
]

# Relative likelihood of an order being for each product - uneven for
# the same reason as REGION_WEIGHTS: some products should visibly
# outsell others in the product-performance table. Same length/order as
# PRODUCTS.
PRODUCT_WEIGHTS = [0.40, 0.25, 0.15, 0.12, 0.08]

STARTING_STOCK = 300

# Chance any given order is a deliberate demand spike (15-40 units in
# one order) rather than a normal 1-3 unit purchase - enough to
# occasionally push a region's hourly order *count* baseline (the thing
# the z-score detector actually watches - see detectors/zscore.py) and
# to stress inventory risk, without dominating the data.
SPIKE_CHANCE = 0.02
SPIKE_QUANTITY_RANGE = (15, 40)
NORMAL_QUANTITY_RANGE = (1, 3)


def seed_inventory(session: requests.Session, base_url: str) -> None:
    endpoint = f"{base_url}/api/inventory/seed"
    print(f"Seeding inventory -> POST {endpoint} ({len(PRODUCTS)} products x {len(REGIONS)} regions)")
    for product in PRODUCTS:
        for region in REGIONS:
            payload = {
                "product_id": product.product_id,
                "product_name": product.product_name,
                "category": product.category,
                "brand": product.brand,
                "region": region,
                # Small spread around STARTING_STOCK so regions don't
                # all start perfectly identical.
                "current_stock": STARTING_STOCK + random.randint(-20, 20),
            }
            try:
                response = session.post(endpoint, json=payload, timeout=5)
                response.raise_for_status()
            except requests.exceptions.RequestException as exc:
                print(f"  FAILED to seed {product.product_id}/{region}: {exc}")
    print("Inventory seeded.\n")


def generate_order(spike: bool) -> dict:
    product = random.choices(PRODUCTS, weights=PRODUCT_WEIGHTS, k=1)[0]
    region = random.choices(REGIONS, weights=REGION_WEIGHTS, k=1)[0]
    quantity = (
        random.randint(*SPIKE_QUANTITY_RANGE) if spike else random.randint(*NORMAL_QUANTITY_RANGE)
    )

    return {
        "order_id": str(uuid.uuid4()),
        "product_id": product.product_id,
        "product_name": product.product_name,
        "category": product.category,
        "brand": product.brand,
        "quantity": quantity,
        "unit_price": product.unit_price,
        "region": region,
    }


def post_order(session: requests.Session, endpoint: str, payload: dict) -> tuple[bool, str]:
    """POSTs one order. Never raises - connection problems are reported
    back as (False, reason) so the caller can log and keep going instead
    of crashing the whole simulator over one bad request.
    """
    try:
        response = session.post(endpoint, json=payload, timeout=5)
    except requests.exceptions.ConnectionError:
        return False, "connection refused - is the backend running?"
    except requests.exceptions.Timeout:
        return False, "request timed out"
    except requests.exceptions.RequestException as exc:
        return False, f"request failed: {exc}"

    if response.status_code == 201:
        return True, ""
    return False, f"unexpected status {response.status_code}: {response.text[:200]}"


def run(base_url: str, interval: float, duration: float | None) -> None:
    session = requests.Session()
    seed_inventory(session, base_url)

    orders_endpoint = f"{base_url}/api/orders"
    event_count = 0
    failure_count = 0
    start = time.monotonic()

    print(f"Simulator started -> POST {orders_endpoint}  (interval={interval}s)")
    print("Synthetic data for demo purposes only - not live production traffic.")
    print("Press Ctrl+C to stop.\n")

    try:
        while duration is None or (time.monotonic() - start) < duration:
            is_spike = random.random() < SPIKE_CHANCE
            payload = generate_order(is_spike)

            ok, error = post_order(session, orders_endpoint, payload)
            event_count += 1
            tag = "*** DEMAND SPIKE ***" if is_spike else "normal"

            if ok:
                print(
                    f"[{event_count:>5}] {payload['product_name']:<24} qty={payload['quantity']:>3}  "
                    f"region={payload['region']:<15} {tag}"
                )
            else:
                failure_count += 1
                print(f"[{event_count:>5}] FAILED  {payload['product_name']:<24} -> {error}")

            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
        print(f"\nStopped after {event_count} events ({failure_count} failed to post).")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--url",
        default=os.environ.get("SIMULATOR_API_URL", "http://localhost:8000"),
        help="Base URL of the backend API (default: http://localhost:8000, or $SIMULATOR_API_URL)",
    )
    parser.add_argument("--interval", type=float, default=1.5, help="Seconds between events (default: 1.5)")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop automatically after N seconds (default: run until Ctrl+C)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(args.url.rstrip("/"), args.interval, args.duration)


if __name__ == "__main__":
    main()
