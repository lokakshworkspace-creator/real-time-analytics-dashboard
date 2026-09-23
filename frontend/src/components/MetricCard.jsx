// One KPI card: a label and a pre-formatted value. Deliberately generic
// (no metric-name lookup, no per-card anomaly/source/timestamp meta) —
// unlike the old system-metrics dashboard, these four cards all come
// from a single GET /api/orders/kpis response, not one document per
// card, so there's no per-card "is this stale" or "is this anomalous"
// state to show.
export function MetricCard({ label, value }) {
  return (
    <div className="metric-card">
      <div className="metric-card__label">{label}</div>
      <div className="metric-card__value">{value}</div>
    </div>
  )
}
