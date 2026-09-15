// Fixed polling interval for the whole dashboard, per CLAUDE.md's
// real-time design decision: polling on a 5s interval, not WebSockets/
// SSE — the backend stays stateless (trivial horizontal scaling) and
// 5s is frequent enough for this app's update cadence. One named
// constant, imported by MetricCardsRow/TrendChart/AnomalyPanel, rather
// than the number 5000 repeated three times.
export const POLL_INTERVAL_MS = 5000
