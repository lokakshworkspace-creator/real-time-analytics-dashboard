import { useCallback, useState } from 'react'
import { getBrandsBenchmark } from '../api/client'
import { POLL_INTERVAL_MS } from '../constants'
import { DeltaBadge } from './DeltaBadge'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency, formatInteger } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RangeToggle } from './RangeToggle'
import { RefreshIndicator } from './RefreshIndicator'

// Admin-only (see App.jsx — rendered only when user.role === 'admin').
// The backend also enforces this independently (require_admin on GET
// /api/brands/benchmark returns 403 for a business account), but a
// business account should never even see the request attempted — a
// competitor's aggregate revenue figures are not something a business
// user should be shown exists, let alone denied access to.
export function BenchmarkTable() {
  const [range, setRange] = useState('30d')
  const fetchFn = useCallback(() => getBrandsBenchmark({ range }), [range])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Cross-Brand Benchmark</h2>
        <div className="section-header__controls">
          <RangeToggle value={range} onChange={setRange} />
          <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
        </div>
      </div>

      {loading && <LoadingState label="Loading benchmark…" />}
      {error && <ErrorState error={error} label="Could not load benchmark" />}

      {!loading && !error && (!data || data.benchmarks.length === 0) && (
        <p className="empty-note">No brands with orders in this range yet.</p>
      )}

      {!loading && !error && data && data.benchmarks.length > 0 && (
        <>
          <p className="panel__footnote">Average revenue across all brands: {formatCurrency(data.average_revenue)}</p>
          <table className="data-table">
            <thead>
              <tr>
                <th>Brand</th>
                <th>Orders</th>
                <th>Revenue</th>
                <th>Avg. Order Value</th>
                <th>vs. average</th>
              </tr>
            </thead>
            <tbody>
              {data.benchmarks.map((b) => (
                <tr key={b.brand}>
                  <td>{b.brand}</td>
                  <td>{formatInteger(b.orders)}</td>
                  <td>{formatCurrency(b.revenue)}</td>
                  <td>{formatCurrency(b.avg_order_value)}</td>
                  <td>
                    <DeltaBadge delta={b.revenue_vs_average_pct} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}
