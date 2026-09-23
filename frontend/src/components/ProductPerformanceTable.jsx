import { useCallback } from 'react'
import { getProductStats } from '../api/client'
import { DEFAULT_WINDOW_MINUTES, POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency, formatInteger } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

const PRODUCT_LIMIT = 5

function ProductList({ products }) {
  if (products.length === 0) {
    return <p className="empty-note">No orders in this window yet.</p>
  }

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Product</th>
          <th>Units Sold</th>
          <th>Revenue</th>
        </tr>
      </thead>
      <tbody>
        {products.map((product) => (
          <tr key={product.product_id}>
            <td>{product.product_name}</td>
            <td>{formatInteger(product.units_sold)}</td>
            <td>{formatCurrency(product.revenue)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function ProductPerformanceTable() {
  const fetchTop = useCallback(
    () => getProductStats(DEFAULT_WINDOW_MINUTES, PRODUCT_LIMIT, 'top'),
    []
  )
  const fetchBottom = useCallback(
    () => getProductStats(DEFAULT_WINDOW_MINUTES, PRODUCT_LIMIT, 'bottom'),
    []
  )

  const top = useApiData(fetchTop, { intervalMs: POLL_INTERVAL_MS })
  const bottom = useApiData(fetchBottom, { intervalMs: POLL_INTERVAL_MS })

  const loading = top.loading || bottom.loading
  const error = top.error ?? bottom.error

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Product Performance — last {DEFAULT_WINDOW_MINUTES} min</h2>
        <RefreshIndicator
          isRefreshing={top.isRefreshing || bottom.isRefreshing}
          pollError={top.pollError ?? bottom.pollError}
          lastUpdated={top.lastUpdated}
        />
      </div>

      {loading && <LoadingState label="Loading product performance…" />}
      {!loading && error && <ErrorState error={error} label="Could not load product performance" />}

      {!loading && !error && (
        <div className="product-performance">
          <div>
            <h3 className="panel__subtitle">Top Sellers</h3>
            <ProductList products={top.data ?? []} />
          </div>
          <div>
            <h3 className="panel__subtitle">Slow Movers</h3>
            <ProductList products={bottom.data ?? []} />
          </div>
        </div>
      )}
    </section>
  )
}
