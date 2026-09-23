"""Anomaly detectors, kept deliberately separate from routers/models.

- zscore.py  The only detector in this app. Runs on every POST
             /api/orders (detection-on-write); its verdict is what gets
             stored as the `anomaly` field. `compute_zscore` is the pure
             math core (unit-tested directly in
             backend/tests/test_zscore.py); `score_order_volume` is the
             async wrapper that fetches a region's hourly order-count
             history from MongoDB before handing off to it.

An Isolation Forest detector (scikit-learn) previously lived here as a
Phase 5b stretch-goal comparison tool, kept deliberately out of the live
ingest path. It was removed when the system-metrics track it scored
(cpu_usage/memory_usage/response_time) was replaced by this
business-analytics track — it scored raw per-event metric values, which
has no direct equivalent now that detection runs on hourly order-volume
buckets per region. Re-adding an alternate/offline detector for this
domain is a documented "next step", not a built feature.
"""
