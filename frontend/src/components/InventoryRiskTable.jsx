import { useCallback } from 'react'
import { getInventoryRisk } from '../api/client'
import { useBrandFilter } from '../context/BrandFilterContext'
import { INVENTORY_RISK_WINDOW_MINUTES, POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatInteger, RISK_LABEL } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { ExportCsvButton } from './ExportCsvButton'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

// Reorder suggestions are only shown for HIGH/MEDIUM risk rows — a
// healthy (LOW risk) product's reorder math is either null (no recent
// demand to project from) or just noise next to rows that actually
// need attention. Skipping it here is a display choice; the API
// itself still returns reorder_suggestion for every row where it's
// computable, regardless of risk tier (see routers/inventory.py).
function ReorderSuggestion({ item }) {
  if (item.risk === 'LOW' || !item.reorder_suggestion) return null

  return (
    <div className="reorder-suggestion">
      Reorder {formatInteger(item.reorder_suggestion.suggested_quantity)} units by{' '}
      {formatReorderDate(item.reorder_suggestion.suggested_by_date)}
    </div>
  )
}

// "2026-10-03" -> "Oct 3" — the table is already dense; a short form
// reads faster than a full date next to a risk badge and a stock count.
function formatReorderDate(isoDate) {
  const date = new Date(`${isoDate}T00:00:00Z`)
  if (Number.isNaN(date.getTime())) return isoDate
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

function formatDaysRemaining(days) {
  if (days === null || days === undefined) return '—'
  return `${days.toFixed(1)}d`
}

export function InventoryRiskTable() {
  const { selectedBrand } = useBrandFilter()
  const brand = selectedBrand || undefined

  const fetchFn = useCallback(
    () => getInventoryRisk({ minutes: INVENTORY_RISK_WINDOW_MINUTES, brand }),
    [brand]
  )
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Inventory Risk</h2>
        <div className="section-header__controls">
          <ExportCsvButton
            path="/inventory/risk"
            params={{ minutes: INVENTORY_RISK_WINDOW_MINUTES, brand }}
            filename="inventory_risk.csv"
          />
          <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
        </div>
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
              <th>Days Left</th>
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
                <td>{formatDaysRemaining(item.days_of_stock_remaining)}</td>
                <td>
                  <span className={`risk-badge risk-badge--${item.risk.toLowerCase()}`}>
                    {RISK_LABEL[item.risk]}
                  </span>
                  <ReorderSuggestion item={item} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
