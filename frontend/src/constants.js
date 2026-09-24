// Fixed polling interval for the whole dashboard, per CLAUDE.md's
// real-time design decision: polling on a 5s interval, not WebSockets/
// SSE — the backend stays stateless (trivial horizontal scaling) and
// 5s is frequent enough for this app's update cadence. One named
// constant, imported by every section, rather than the number 5000
// repeated in each one.
export const POLL_INTERVAL_MS = 5000

// Default trailing period for KPIs/regions/products/trend — matches
// the backend's own default (routers/orders.py's range Query default).
export const DEFAULT_RANGE = '7d'

// Inventory risk looks back further than the KPI/regional windows above
// on purpose: "recent demand" for a restocking decision is a
// meaningfully longer horizon (a day) than "what's trending this hour".
export const INVENTORY_RISK_WINDOW_MINUTES = 60 * 24
