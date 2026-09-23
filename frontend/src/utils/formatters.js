// Presentational formatting shared across the dashboard's sections —
// the API returns plain numbers and ISO timestamps; how they're
// displayed (currency symbol, thousands separators, relative time) is
// decided here, not something the backend needs to know about.

const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function formatCurrency(value) {
  return currencyFormatter.format(value ?? 0)
}

const integerFormatter = new Intl.NumberFormat('en-US')

export function formatInteger(value) {
  return integerFormatter.format(value ?? 0)
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

export const RISK_LABEL = {
  HIGH: 'High',
  MEDIUM: 'Medium',
  LOW: 'Low',
}
