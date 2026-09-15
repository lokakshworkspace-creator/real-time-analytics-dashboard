// The 5 metrics this app knows about, in a fixed display order —
// mirrors backend/app/models.py's METRIC_NAMES. Duplicated rather than
// shared: frontend and backend are separate language runtimes here,
// so there's no single source of truth to import from without adding
// a build step just to share one list of strings.
export const METRIC_NAMES = ['orders', 'response_time', 'cpu_usage', 'failed_requests', 'memory_usage']

// Display-only metadata (label + unit). The API never returns a unit —
// value is just a number — so this is purely presentational, decided
// here in the frontend, not something the backend needs to know about.
export const METRIC_DISPLAY = {
  orders: { label: 'Orders', unit: '' },
  response_time: { label: 'Response Time', unit: 'ms' },
  cpu_usage: { label: 'CPU Usage', unit: '%' },
  failed_requests: { label: 'Failed Requests', unit: '' },
  memory_usage: { label: 'Memory Usage', unit: '%' },
}

export function formatMetricValue(metric, value) {
  const unit = METRIC_DISPLAY[metric]?.unit ?? ''
  // response_time/orders/failed_requests are whole numbers in practice;
  // cpu_usage/memory_usage carry one decimal (see simulator.py's
  // `decimals` per metric) — round to at most 1 decimal place so the
  // display never shows more false precision than the source data has.
  const rounded = Math.round(value * 10) / 10
  return unit ? `${rounded}${unit}` : `${rounded}`
}

// "5s ago" / "3m ago" / "2h ago" — deliberately coarse (no library):
// this app doesn't need calendar-aware relative time ("yesterday",
// "last week"), just "how stale is this card", so a small manual
// threshold ladder is simpler to read than pulling in a date library.
export function formatRelativeTime(isoTimestamp) {
  const then = new Date(isoTimestamp).getTime()
  if (Number.isNaN(then)) return 'unknown'

  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (seconds < 5) return 'just now'
  if (seconds < 60) return `${seconds}s ago`

  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`

  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`

  const days = Math.round(hours / 24)
  return `${days}d ago`
}

export function formatClockTime(isoTimestamp) {
  const date = new Date(isoTimestamp)
  if (Number.isNaN(date.getTime())) return '--:--:--'
  return date.toLocaleTimeString([], { hour12: false })
}
