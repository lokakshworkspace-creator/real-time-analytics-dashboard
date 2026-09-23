import { useCallback } from 'react'
import { getOrderKpis } from '../api/client'
import { DEFAULT_WINDOW_MINUTES, POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency, formatInteger } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { MetricCard } from './MetricCard'
import { RefreshIndicator } from './RefreshIndicator'

export function KpiCardsRow() {
  const fetchFn = useCallback(() => getOrderKpis(DEFAULT_WINDOW_MINUTES), [])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  // Only reachable before the first successful fetch ever completes —
  // once `data` exists, later poll failures never reach here (see
  // useApiData), so the KPI row is never blanked by a bad poll.
  if (loading) return <LoadingState label="Loading KPIs…" />
  if (error) return <ErrorState error={error} label="Could not load KPIs" />

  // GET /api/orders/kpis always returns zeros (not 404) for an empty
  // window, so `data` is never null here once loading/error have
  // cleared — see models.py's OrderKpis docstring for why.
  return (
    <section className="metric-cards-section">
      <div className="section-header">
        <h2 className="panel__title">Orders — last {data.minutes} min</h2>
        <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
      </div>
      <div className="metric-cards-row">
        <MetricCard label="Total Orders" value={formatInteger(data.total_orders)} />
        <MetricCard label="Revenue" value={formatCurrency(data.revenue)} />
        <MetricCard label="Units Sold" value={formatInteger(data.units_sold)} />
        <MetricCard label="Avg Order Value" value={formatCurrency(data.avg_order_value)} />
      </div>
    </section>
  )
}
