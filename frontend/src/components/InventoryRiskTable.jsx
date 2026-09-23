import { useCallback } from 'react'
import { getInventoryRisk } from '../api/client'
import { INVENTORY_RISK_WINDOW_MINUTES, POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatInteger, RISK_LABEL } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

export function InventoryRiskTable() {
  const fetchFn = useCallback(() => getInventoryRisk(INVENTORY_RISK_WINDOW_MINUTES), [])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Inventory Risk</h2>
        <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
      </div>

      {loading && <LoadingState label="Loading inventory risk…" />}
      {error && <ErrorState error={error} label="Could not load inventory risk" />}

      {!loading && !error && (!data || data.length === 0) && (
        <p className="empty-note">No inventory records yet — seed inventory via the simulator.</p>
      )}

      {!loading && !error && data && data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Product</th>
              <th>Region</th>
              <th>Stock</th>
              <th>Recent Demand</th>
              <th>Risk</th>
            </tr>
          </thead>
          <tbody>
            {data.map((item) => (
              <tr key={`${item.product_id}-${item.region}`}>
                <td>{item.product_name}</td>
                <td>{item.region}</td>
                <td>{formatInteger(item.current_stock)}</td>
                <td>{formatInteger(item.recent_demand)}</td>
                <td>
                  <span className={`risk-badge risk-badge--${item.risk.toLowerCase()}`}>
                    {RISK_LABEL[item.risk]}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
