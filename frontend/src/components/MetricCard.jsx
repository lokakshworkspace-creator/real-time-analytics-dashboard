import { formatMetricValue, formatRelativeTime, METRIC_DISPLAY } from '../utils/formatters'

// One card. `entry` is the matching object from GET /metrics/latest,
// or null if that metric has no data yet (see MetricCardsRow) — Phase
// 4 deliberately omits metrics with zero events rather than padding
// the response, so the frontend is the layer that turns "absent" into
// an honest "No data yet" card instead of just not showing a 5th card
// at all (fixed 5-card layout, not a shifting one).
export function MetricCard({ metric, entry }) {
  const { label } = METRIC_DISPLAY[metric] ?? { label: metric }

  return (
    <div className={`metric-card${entry?.anomaly ? ' metric-card--anomaly' : ''}`}>
      <div className="metric-card__label">{label}</div>

      {entry ? (
        <>
          <div className="metric-card__value">{formatMetricValue(metric, entry.value)}</div>
          <div className="metric-card__meta">
            <span>{entry.source}</span>
            <span>{formatRelativeTime(entry.timestamp)}</span>
          </div>
          {entry.anomaly && <div className="metric-card__badge">Anomaly</div>}
        </>
      ) : (
        <div className="metric-card__empty">No data yet</div>
      )}
    </div>
  )
}
