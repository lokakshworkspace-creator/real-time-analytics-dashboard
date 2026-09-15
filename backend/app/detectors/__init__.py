"""Anomaly detectors, kept deliberately separate from routers/models.

Two independently-testable modules, per CLAUDE.md's ground rule that the
live detector must always be unambiguous:

- zscore.py            The MVP detector. Runs on every POST /api/metrics
                        (detection-on-write); its verdict is what gets
                        stored as the `anomaly` field.
- isolation_forest.py  Phase 5b stretch. Never runs automatically and
                        never touches the `anomaly` field — it's an
                        on-demand, offline comparison tool, invoked
                        separately (see its own docstring).

Nothing here imports the other detector module, and neither imports
from routers/ — both take plain values in, return a verdict out, so
either can be unit-tested with no FastAPI or MongoDB involved beyond
what each explicitly wires up itself.
"""
