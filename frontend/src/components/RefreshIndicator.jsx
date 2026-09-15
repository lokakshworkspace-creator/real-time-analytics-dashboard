import { formatRelativeTime } from '../utils/formatters'

// Small, non-disruptive indicator for what a poll tick is doing —
// deliberately NOT a spinner or anything that replaces the section's
// content. Renders nothing when there's nothing worth mentioning (not
// currently refreshing, and the last poll succeeded).
export function RefreshIndicator({ isRefreshing, pollError, lastUpdated }) {
  if (pollError) {
    return (
      <span className="refresh-indicator refresh-indicator--error" title={pollError.message}>
        <span className="refresh-indicator__icon" aria-hidden="true">
          ⚠
        </span>
        Trouble refreshing
        {lastUpdated ? ` — showing data from ${formatRelativeTime(lastUpdated)}` : ''}
      </span>
    )
  }

  if (isRefreshing) {
    return (
      <span className="refresh-indicator refresh-indicator--active" role="status" aria-live="polite">
        <span className="refresh-dot" aria-hidden="true" />
        Refreshing…
      </span>
    )
  }

  return null
}
