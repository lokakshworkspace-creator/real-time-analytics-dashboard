import { useCallback, useState } from 'react'
import { getProductStats } from '../api/client'
import { useBrandFilter } from '../context/BrandFilterContext'
import { POLL_INTERVAL_MS } from '../constants'
import { DeltaBadge } from './DeltaBadge'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency, formatInteger } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { ExportCsvButton } from './ExportCsvButton'
import { LoadingState } from './LoadingState'
import { RangeToggle } from './RangeToggle'
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
          <th>vs. previous</th>
        </tr>
      </thead>
      <tbody>
        {products.map((product) => (
          <tr key={product.product_id}>
            <td>{product.product_name}</td>
            <td>{formatInteger(product.current.units_sold)}</td>
            <td>{formatCurrency(product.current.revenue)}</td>
            <td>
              <DeltaBadge delta={product.change_pct.revenue} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function ProductPerformanceTable() {
  const [range, setRange] = useState('7d')
  const { selectedBrand } = useBrandFilter()
  const brand = selectedBrand || undefined

  const fetchTop = useCallback(
    () => getProductStats({ range, limit: PRODUCT_LIMIT, order: 'top', brand }),
    [range, brand]
  )
  const fetchBottom = useCallback(
    () => getProductStats({ range, limit: PRODUCT_LIMIT, order: 'bottom', brand }),
    [range, brand]
  )

  const top = useApiData(fetchTop, { intervalMs: POLL_INTERVAL_MS })
  const bottom = useApiData(fetchBottom, { intervalMs: POLL_INTERVAL_MS })

  const loading = top.loading || bottom.loading
  const error = top.error ?? bottom.error
  // GET /api/orders/products?order=top and ?order=bottom each cap
  // their own list to a non-overlapping half of the catalog when
  // there aren't enough distinct products for the requested limit
  // (see routers/orders.py's get_product_stats) — `note` shows up
  // whenever that capping actually happened, so a shorter-than-
  // requested list reads as "the catalog's this small" rather than
  // looking broken. Either direction's note describes both halves, so
  // showing just one (if present) is enough.
  const note = top.data?.note ?? bottom.data?.note

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Product Performance</h2>
        <div className="section-header__controls">
          <RangeToggle value={range} onChange={setRange} />
          <ExportCsvButton
            path="/orders/products"
            params={{ range, order: 'top', limit: 100, brand }}
            filename="product_performance.csv"
          />
          <RefreshIndicator
            isRefreshing={top.isRefreshing || bottom.isRefreshing}
            pollError={top.pollError ?? bottom.pollError}
            lastUpdated={top.lastUpdated}
          />
        </div>
      </div>

      {loading && <LoadingState label="Loading product performance…" />}
      {!loading && error && <ErrorState error={error} label="Could not load product performance" />}

      {!loading && !error && (
        <>
          {note && <p className="panel__note">{note}</p>}
          <div className="product-performance">
            <div>
              <h3 className="panel__subtitle">Top Sellers</h3>
              <ProductList products={top.data?.products ?? []} />
            </div>
            <div>
              <h3 className="panel__subtitle">Slow Movers</h3>
              <ProductList products={bottom.data?.products ?? []} />
            </div>
          </div>
        </>
      )}
    </section>
  )
}
