import { useCallback } from 'react'
import { getOrders } from '../api/client'
import { useBrandFilter } from '../context/BrandFilterContext'
import { POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency, formatInteger, formatRelativeTime } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { ExportCsvButton } from './ExportCsvButton'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

const ORDER_LIMIT = 10

export function RecentOrdersTable() {
  const { selectedBrand } = useBrandFilter()
  const brand = selectedBrand || undefined

  const fetchFn = useCallback(() => getOrders({ limit: ORDER_LIMIT, brand }), [brand])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Recent Orders</h2>
        <div className="section-header__controls">
          <ExportCsvButton
            path="/orders"
            params={{ limit: 500, brand }}
            filename="orders.csv"
          />
          <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
        </div>
      </div>

      {loading && <LoadingState label="Loading recent orders…" />}
      {error && <ErrorState error={error} label="Could not load recent orders" />}

      {!loading && !error && (!data || data.length === 0) && (
        <p className="empty-note">No orders yet — run the simulator to generate some.</p>
      )}

      {!loading && !error && data && data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Product</th>
              <th>Region</th>
              <th>Qty</th>
              <th>Total</th>
              <th>When</th>
            </tr>
          </thead>
          <tbody>
            {data.map((order) => (
              <tr key={order.id} className={order.anomaly ? 'data-table__row--flagged' : undefined}>
                <td>{order.product_name}</td>
                <td>{order.region}</td>
                <td>{formatInteger(order.quantity)}</td>
                <td>{formatCurrency(order.total_value)}</td>
                <td>{formatRelativeTime(order.timestamp)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
