"""Backfills a realistic order history for one brand (default: Nike), so
the dashboard has something worth browsing the moment you log in as that
brand's business account: KPIs with a real previous period to compare
against, a trend with weekday/weekend shape, regions and products that
genuinely differ, an inventory table with a range of stock levels, and a
couple of real demand events for the anomaly detectors to find.

Like simulator.py and seed_users.py, this is an HTTP client of the API —
never touches MongoDB directly — so every order goes through the same
validation, z-score detection and inventory decrement a live order would.
Orders are posted in CHRONOLOGICAL order for that reason: the z-score is
computed as each order arrives against the region's history so far, so
posting out of order would give verdicts no live system could have
produced.

What the history contains (all of it derived from simulator.PRODUCTS, so
the catalog has one source of truth):
  - the last --days days (default 14: this week AND the previous week, so
    every period-over-period delta on the dashboard has both sides),
    ending at "now";
  - per product x region daily volumes that differ on purpose (shoes sell
    well everywhere, bags do best in Europe and the Middle East, the cap
    sells best in Asia Pacific), a region-specific time-of-day peak, and
    a weekend lift;
  - deliberate week-over-week trends: shoes and bags growing, the cap
    declining;
  - two real-looking demand events (a t-shirt promotion in Europe, and a
    one-hour bulk buy of duffel bags in the Middle East) — ordinary
    orders with ordinary uuid order_ids, no test markers;
  - varied final stock levels sized off each product+region's actual
    demand, including one HIGH-risk and one MEDIUM-risk item.

Idempotency: NOT idempotent — re-running adds another history on top.
Order ids are random uuids, so nothing collides, but totals double.

Run:   python seed_history.py [--brand Nike] [--days 14] [--url URL] [--seed N]
Then:  run the detector batch so the events get scored by all three
       detectors (admin token):
         POST /api/detectors/run-batch?window_days=14
"""

from __future__ import annotations

import argparse
import math
import os
import random
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from simulator import PRODUCTS, REGIONS, Product

# Average orders per day, per (product_id, region), for the CURRENT week.
# Written out (not derived from weights) because the whole point is that
# the shape differs by product and region in ways a formula would smooth
# away: shoes are a strong seller everywhere, the duffel bag is a Europe
# and Middle East product, the cap does best in Asia Pacific.
BASE_DAILY_ORDERS: dict[str, dict[str, float]] = {
    "sku-001": {"North America": 22, "Europe": 18, "Asia Pacific": 14, "Latin America": 7, "Middle East": 5},  # shoes
    "sku-006": {"North America": 12, "Europe": 10, "Asia Pacific": 9, "Latin America": 6, "Middle East": 4},  # t-shirt
    "sku-007": {"North America": 4, "Europe": 9, "Asia Pacific": 3, "Latin America": 2, "Middle East": 8},  # duffel bag
    "sku-008": {"North America": 6, "Europe": 5, "Asia Pacific": 8, "Latin America": 4, "Middle East": 2},  # cap
}

# Volume in the PREVIOUS week as a multiple of the current week's — so a
# factor below 1 means the product is growing, above 1 means declining.
# Default 1.0 (flat). These give the dashboard both green and red deltas.
PREVIOUS_WEEK_FACTOR_DEFAULT: dict[str, float] = {
    "sku-001": 0.88,  # shoes: up ~14% week over week
    "sku-006": 1.0,  # t-shirt: flat
    "sku-007": 1.0,  # duffel bag: flat overall...
    "sku-008": 1.4,  # cap: down ~29% week over week
}
PREVIOUS_WEEK_FACTOR_OVERRIDES: dict[tuple[str, str], float] = {
    ("sku-001", "Latin America"): 0.70,  # shoes growing fastest in Latin America
    ("sku-007", "Europe"): 0.75,  # ...but the bag is growing in Europe and
    ("sku-007", "Middle East"): 0.70,  # the Middle East specifically
    ("sku-008", "Middle East"): 1.9,  # cap collapsing in the Middle East
}

# UTC hour at which each region's orders peak (rough local evening).
PEAK_HOUR_UTC = {
    "North America": 0,
    "Europe": 17,
    "Asia Pacific": 9,
    "Latin America": 23,
    "Middle East": 15,
}

WEEKEND_LIFT = 1.2

# Typical units per order, per product: (quantities, weights).
QUANTITY_MIX: dict[str, tuple[list[int], list[float]]] = {
    "sku-001": ([1, 2, 3], [0.80, 0.15, 0.05]),
    "sku-006": ([1, 2, 3], [0.60, 0.28, 0.12]),
    "sku-007": ([1, 2], [0.85, 0.15]),
    "sku-008": ([1, 2, 3], [0.70, 0.25, 0.05]),
}


def hourly_weights(region: str) -> list[float]:
    """Relative order volume for each UTC hour of the day, mean 1.0: a
    smooth day/night curve peaking at the region's local evening, never
    reaching zero (people order at 4am too).
    """
    peak = PEAK_HOUR_UTC[region]
    raw = [0.25 + 0.75 * (1 + math.cos(2 * math.pi * (h - peak) / 24)) / 2 for h in range(24)]
    mean = sum(raw) / 24
    return [w / mean for w in raw]


def poisson(rng: random.Random, lam: float) -> int:
    """Knuth's algorithm — fine for the small per-hour rates here (< ~5)."""
    if lam <= 0:
        return 0
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def build_orders(brand_products: list[Product], days: int, now: datetime, rng: random.Random) -> list[dict]:
    """Every order for the window, oldest first. Two kinds: the regular
    Poisson-sampled flow, plus the two explicit demand events."""
    start = (now - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
    orders: list[tuple[datetime, Product, str, int]] = []

    # Demand events. Hours-back are relative to `now` and deliberately
    # RECENT: the anomaly panel lists the newest 50 flagged orders and the
    # batch scores the last 24 hours by default, so an event from two days
    # ago would be both unscored and buried under newer rows.
    promo_hours = {(now - timedelta(hours=h)).replace(minute=0, second=0, microsecond=0) for h in (8, 7, 6)}
    promo_multiplier = 12  # Nike Dri-FIT T-Shirt promotion in Europe

    hour = start
    while hour <= now:
        for product in brand_products:
            per_region = BASE_DAILY_ORDERS[product.product_id]
            for region in REGIONS:
                base = per_region[region]
                # Which week is this hour in? The last 7 days are "current";
                # anything earlier is the previous week.
                in_current_week = hour >= now - timedelta(days=7)
                week_factor = 1.0 if in_current_week else PREVIOUS_WEEK_FACTOR_OVERRIDES.get(
                    (product.product_id, region), PREVIOUS_WEEK_FACTOR_DEFAULT[product.product_id]
                )
                day_factor = WEEKEND_LIFT if hour.weekday() >= 5 else 1.0
                lam = base / 24 * hourly_weights(region)[hour.hour] * week_factor * day_factor
                if product.product_id == "sku-006" and region == "Europe" and hour in promo_hours:
                    lam *= promo_multiplier
                # The current, still-partial hour only gets its elapsed share.
                if hour.replace(minute=0, second=0, microsecond=0) == now.replace(minute=0, second=0, microsecond=0):
                    lam *= (now - hour).total_seconds() / 3600
                quantities, weights = QUANTITY_MIX[product.product_id]
                for _ in range(poisson(rng, lam)):
                    ts = hour + timedelta(seconds=rng.uniform(0, 3599))
                    if ts <= now:
                        orders.append((ts, product, region, rng.choices(quantities, weights)[0]))
        hour += timedelta(hours=1)

    # Event 2: a one-hour bulk buy of duffel bags in the Middle East
    # (16 orders, 2-4 units each) a few hours ago — well past what the
    # region normally sees in an hour.
    bag = next(p for p in brand_products if p.product_id == "sku-007")
    burst_hour = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    for i in range(16):
        ts = burst_hour + timedelta(seconds=90 + i * 200 + rng.uniform(0, 60))
        orders.append((ts, bag, "Middle East", rng.choice([2, 3, 4])))

    orders.sort(key=lambda o: o[0])
    return [
        {
            "order_id": str(uuid.uuid4()),
            "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "product_id": product.product_id,
            "product_name": product.product_name,
            "category": product.category,
            "brand": product.brand,
            "quantity": qty,
            "unit_price": product.unit_price,
            "region": region,
        }
        for ts, product, region, qty in orders
    ]


def final_stock_levels(
    brand_products: list[Product], orders: list[dict], now: datetime, rng: random.Random
) -> dict[tuple[str, str], int]:
    """Stock per (product_id, region), sized off that combination's real
    demand: a random 6-30 days of cover on its 7-day average, so levels
    differ realistically — then one HIGH-risk item (the busiest product+
    region of the last 24h, nearly sold out) and one MEDIUM-risk item (a
    slow mover that's low but not yet outrunning demand).
    """
    last_24h = now - timedelta(hours=24)
    last_7d = now - timedelta(days=7)
    units_24h: dict[tuple[str, str], int] = defaultdict(int)
    units_7d: dict[tuple[str, str], int] = defaultdict(int)
    for o in orders:
        ts = datetime.strptime(o["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        key = (o["product_id"], o["region"])
        if ts >= last_7d:
            units_7d[key] += o["quantity"]
        if ts >= last_24h:
            units_24h[key] += o["quantity"]

    stock = {}
    for p in brand_products:
        for region in REGIONS:
            key = (p.product_id, region)
            avg_daily_units = units_7d[key] / 7
            stock[key] = max(25, round(avg_daily_units * rng.uniform(6, 30)))

    busiest = max(stock, key=lambda k: units_24h[k])
    stock[busiest] = max(3, min(18, units_24h[busiest] - 1))  # HIGH: <= 20 and demand >= stock
    quietest = min((k for k in stock if k != busiest and units_7d[k] > 0), key=lambda k: units_7d[k])
    stock[quietest] = 14 if units_24h[quietest] < 14 else 20  # MEDIUM: low, but demand hasn't caught up
    return stock


def seed_inventory(session: requests.Session, base_url: str, product: Product, region: str, stock: int) -> None:
    session.post(
        f"{base_url}/api/inventory/seed",
        json={
            "product_id": product.product_id,
            "product_name": product.product_name,
            "category": product.category,
            "brand": product.brand,
            "region": region,
            "current_stock": stock,
        },
        timeout=10,
    ).raise_for_status()


def run(base_url: str, brand: str, days: int, seed: int) -> None:
    brand_products = [p for p in PRODUCTS if p.brand == brand]
    if not brand_products or not all(p.product_id in BASE_DAILY_ORDERS for p in brand_products):
        raise SystemExit(
            f"No demand profile for brand {brand!r}: BASE_DAILY_ORDERS covers only the Nike catalog "
            "(sku-001, sku-006, sku-007, sku-008). Add profiles there to seed another brand."
        )

    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    session = requests.Session()

    # Stock everything generously first so no order in the backfill ever
    # trips the "no inventory record" / stockout warnings; realistic
    # levels are set at the end, once real demand is known.
    print(f"Pre-seeding inventory: {len(brand_products)} products x {len(REGIONS)} regions")
    for p in brand_products:
        for region in REGIONS:
            seed_inventory(session, base_url, p, region, 5000)

    orders = build_orders(brand_products, days, now, rng)
    print(f"Generated {len(orders)} {brand} orders over {days} days ({orders[0]['timestamp']} -> {orders[-1]['timestamp']})")

    failed = 0
    for i, payload in enumerate(orders, start=1):
        response = session.post(f"{base_url}/api/orders", json=payload, timeout=10)
        if response.status_code != 201:
            failed += 1
            print(f"  FAILED {payload['order_id']}: {response.status_code} {response.text[:120]}")
        if i % 500 == 0:
            print(f"  posted {i}/{len(orders)}")
    print(f"Posted {len(orders) - failed} orders ({failed} failed).")

    stock = final_stock_levels(brand_products, orders, now, rng)
    for (product_id, region), level in sorted(stock.items()):
        product = next(p for p in brand_products if p.product_id == product_id)
        seed_inventory(session, base_url, product, region, level)
    print("Final stock set:")
    for p in brand_products:
        print(f"  {p.product_name:<22} " + "  ".join(f"{r.split()[0][:5]}={stock[(p.product_id, r)]:>4}" for r in REGIONS))

    print("\nNext: run the detector batch so the demand events get scored (admin token):")
    print(f"  POST {base_url}/api/detectors/run-batch?window_days={days}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("SIMULATOR_API_URL", "http://localhost:8000"))
    parser.add_argument("--brand", default="Nike")
    parser.add_argument("--days", type=int, default=14, help="History length; 14 covers this week and the previous one.")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed — same seed, same history shape.")
    args = parser.parse_args()
    run(args.url.rstrip("/"), args.brand, args.days, args.seed)


if __name__ == "__main__":
    main()
