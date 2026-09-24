import { DeltaBadge } from './DeltaBadge'

// One KPI card: a label, a pre-formatted value, and an optional
// period-over-period delta. `delta` is a plain percentage number
// (already computed server-side — see OrderKpis.change_pct), null when
// the previous period had nothing to compare against, or omitted
// entirely for a card that doesn't have period comparison at all.
export function MetricCard({ label, value, delta }) {
  return (
    <div className="metric-card">
      <div className="metric-card__label">{label}</div>
      <div className="metric-card__value">{value}</div>
      <DeltaBadge delta={delta} />
    </div>
  )
}
