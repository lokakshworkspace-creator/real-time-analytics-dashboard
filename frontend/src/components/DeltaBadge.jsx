// Shared period-over-period delta indicator — originally lived inside
// MetricCard.jsx (KPI cards only); pulled out once RegionsChart and
// ProductPerformanceTable's rows needed the exact same ▲/▼ + percent
// treatment (Feature 2's per-region/per-product growth rate), so
// there's one visual language for "vs. previous period" across the
// whole dashboard instead of three near-identical implementations.
export function DeltaBadge({ delta }) {
  if (delta === undefined) return null

  if (delta === null) {
    return (
      <span className="metric-card__delta metric-card__delta--neutral" title="No prior period to compare">
        New
      </span>
    )
  }

  const isUp = delta >= 0
  return (
    <span className={`metric-card__delta metric-card__delta--${isUp ? 'up' : 'down'}`}>
      {isUp ? '▲' : '▼'} {Math.abs(delta).toFixed(1)}%
    </span>
  )
}
