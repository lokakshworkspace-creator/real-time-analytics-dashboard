"""Synthetic event producer for the Real-Time Data Analytics Dashboard.

Posts one metric event per tick to POST /api/metrics over plain HTTP -
this script never imports from `app/` or touches MongoDB directly. That's
deliberate: in the real architecture (see CLAUDE.md), a metric producer
is just another client of the API, same as any other service would be.
Keeping the simulator an HTTP client instead of a DB writer means it
exercises the exact same validation/storage path a real producer would
hit, and it can run against a remote/deployed backend later with zero
changes.

Lives at backend/simulator.py rather than a top-level `simulator/`
folder: it's a single ~200-line script with one dependency (`requests`,
already installable into the same venv as the backend), not a project
of its own. A dedicated top-level folder would suggest more structure
than one file needs; living next to `app/` keeps it discoverable without
implying it's part of the FastAPI package (it doesn't import `app` at
all - only reachability, HTTP POST).

Run:    python simulator.py [--url URL] [--interval SECONDS] [--duration SECONDS]
Stop:   Ctrl+C - the loop catches KeyboardInterrupt, closes its HTTP
        session, and prints a summary. No subprocesses/threads are
        spawned, so there's nothing that can be left orphaned.
"""

from __future__ import annotations

import argparse
import os
import random
import time
from dataclasses import dataclass

import requests

SOURCES = ["server-1", "server-2", "server-3"]

# How many normal events occur, on average, between deliberate spikes.
# Re-randomized after every spike so the cadence isn't perfectly
# periodic (a real anomaly detector shouldn't be able to learn "every
# exactly 40th event is bad").
SPIKE_EVERY_MIN = 30
SPIKE_EVERY_MAX = 50


@dataclass(frozen=True)
class MetricProfile:
    """Describes one metric's believable everyday range and its
    deliberately-way-outside-that-range spike range.

    normal_mean/normal_stdev drive a Gaussian, clipped to normal_range,
    so values wander with a realistic shape (some fluctuation, no wild
    jumps) instead of being uniform noise. spike_range is intentionally
    far from normal_range in every case - the ask was for spikes a
    z-score detector obviously should catch, not borderline values.
    """

    normal_range: tuple[float, float]
    normal_mean: float
    normal_stdev: float
    spike_range: tuple[float, float]
    unit: str
    decimals: int = 1


# Ranges below are illustrative - chosen to *look and move* like a real
# system's metrics, not measured from one. There is no production
# system behind this data; see the README's "synthetic data" note.
METRIC_PROFILES: dict[str, MetricProfile] = {
    "cpu_usage": MetricProfile(
        normal_range=(40, 75), normal_mean=57, normal_stdev=8,
        spike_range=(92, 99), unit="%", decimals=1,
    ),
    "response_time": MetricProfile(
        normal_range=(150, 250), normal_mean=200, normal_stdev=20,
        spike_range=(800, 1500), unit="ms", decimals=0,
    ),
    "memory_usage": MetricProfile(
        normal_range=(50, 80), normal_mean=65, normal_stdev=6,
        spike_range=(95, 99), unit="%", decimals=1,
    ),
    "failed_requests": MetricProfile(
        normal_range=(0, 5), normal_mean=2, normal_stdev=1.5,
        spike_range=(40, 80), unit="count", decimals=0,
    ),
    # Orders spikes *down*, not up: a sudden collapse to near-zero is
    # the anomaly that actually matters operationally (an outage or a
    # broken checkout flow), whereas an unusually busy period is a good
    # day, not an incident. Modeling both directions the same way would
    # be the "no cargo-culting" violation CLAUDE.md warns about.
    "orders": MetricProfile(
        normal_range=(20, 80), normal_mean=50, normal_stdev=15,
        spike_range=(0, 2), unit="count", decimals=0,
    ),
}


def generate_value(profile: MetricProfile, spike: bool) -> float:
    if spike:
        value = random.uniform(*profile.spike_range)
    else:
        value = random.gauss(profile.normal_mean, profile.normal_stdev)
        low, high = profile.normal_range
        value = max(low, min(high, value))  # keep "normal" believable, not just Gaussian tails
    return round(value, profile.decimals)


def post_metric(session: requests.Session, endpoint: str, payload: dict) -> tuple[bool, str]:
    """POSTs one event. Never raises - connection problems are reported
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


def run(endpoint: str, interval: float, duration: float | None) -> None:
    session = requests.Session()
    event_count = 0
    failure_count = 0
    events_since_spike = 0
    next_spike_at = random.randint(SPIKE_EVERY_MIN, SPIKE_EVERY_MAX)
    start = time.monotonic()

    print(f"Simulator started -> POST {endpoint}  (interval={interval}s)")
    print("Synthetic data for demo purposes only - not live production traffic.")
    print("Press Ctrl+C to stop.\n")

    try:
        while duration is None or (time.monotonic() - start) < duration:
            metric_name = random.choice(list(METRIC_PROFILES))
            profile = METRIC_PROFILES[metric_name]
            source = random.choice(SOURCES)

            events_since_spike += 1
            is_spike = events_since_spike >= next_spike_at
            if is_spike:
                events_since_spike = 0
                next_spike_at = random.randint(SPIKE_EVERY_MIN, SPIKE_EVERY_MAX)

            value = generate_value(profile, is_spike)
            payload = {"metric": metric_name, "value": value, "source": source}

            ok, error = post_metric(session, endpoint, payload)
            event_count += 1
            tag = "*** SPIKE ***" if is_spike else "normal"

            if ok:
                print(
                    f"[{event_count:>5}] {metric_name:<15} {value:>8}{profile.unit:<5} "
                    f"source={source:<10} {tag}"
                )
            else:
                failure_count += 1
                print(f"[{event_count:>5}] FAILED  {metric_name:<15} source={source:<10} -> {error}")

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
    endpoint = args.url.rstrip("/") + "/api/metrics"
    run(endpoint, args.interval, args.duration)


if __name__ == "__main__":
    main()
