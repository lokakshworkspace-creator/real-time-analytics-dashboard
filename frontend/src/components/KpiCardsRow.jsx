import { useCallback, useState } from 'react'
import { getOrderKpis } from '../api/client'
import { useBrandFilter } from '../context/BrandFilterContext'
import { POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency, formatInteger } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { MetricCard } from './MetricCard'
import { RangeToggle } from './RangeToggle'
import { RefreshIndicator } from './RefreshIndicator'

export function KpiCardsRow() {
  const [range, setRange] = useState('7d')
  const { selectedBrand } = useBrandFilter()

  const fetchFn = useCallback(
    () => getOrderKpis({ range, brand: selectedBrand || undefined }),
    [range, selectedBrand]
  )
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  return (
    <section className="metric-cards-section">
      <div className="section-header">
        <h2 className="panel__title">Orders</h2>
        <div className="section-header__controls">
          <RangeToggle value={range} onChange={setRange} />
          <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
        </div>
      </div>

      {loading && <LoadingState label="Loading KPIs…" />}
      {error && <ErrorState error={error} label="Could not load KPIs" />}

      {/* GET /api/orders/kpis always returns zeros (not 404) for an
          empty window, so `data` is never null here once loading/error
          have cleared — see models.py's OrderKpis docstring. */}
      {!loading && !error && data && (
        <div className="metric-cards-row">
          <MetricCard
            label="Total Orders"
            value={formatInteger(data.current.total_orders)}
            delta={data.change_pct.total_orders}
          />
          <MetricCard
            label="Revenue"
            value={formatCurrency(data.current.revenue)}
            delta={data.change_pct.revenue}
          />
          <MetricCard
            label="Units Sold"
            value={formatInteger(data.current.units_sold)}
            delta={data.change_pct.units_sold}
          />
          <MetricCard
            label="Avg Order Value"
            value={formatCurrency(data.current.avg_order_value)}
            delta={data.change_pct.avg_order_value}
          />
        </div>
      )}
    </section>
  )
}
