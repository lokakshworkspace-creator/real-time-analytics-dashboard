import { useCallback } from 'react'
import { getAlerts } from '../api/client'
import { POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatRelativeTime, SEVERITY_LABEL } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

const TYPE_LABEL = {
  anomaly: 'Anomaly',
  low_stock: 'Low Stock',
  decline: 'Decline',
}

// Additive alongside AnomalyPanel (kept as-is for its detailed
// region-hour anomaly view — z-score, severity, per-event) — this is
// the consolidated, at-a-glance feed over all three signal sources
// (anomalies + HIGH-risk inventory + declining regions/products), the
// "what needs my attention right now" view. See routers/alerts.py.
export function AlertsCenter() {
  const fetchFn = useCallback(() => getAlerts(), [])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Alerts</h2>
        <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
      </div>

      {loading && <LoadingState label="Loading alerts…" />}
      {error && <ErrorState error={error} label="Could not load alerts" />}

      {!loading && !error && (data === null || data.length === 0) && (
        <p className="empty-note">No active alerts — anomalies, low stock, and declining performance all clear.</p>
      )}

      {!loading && !error && data && data.length > 0 && (
        <ul className="anomaly-list">
          {data.map((alert, index) => (
            <li key={index} className="anomaly-item">
              <div className="anomaly-item__row">
                <span className={`severity-badge severity-badge--${alert.severity}`}>
                  {SEVERITY_LABEL[alert.severity]}
                </span>
                <span className="alert-type-badge">{TYPE_LABEL[alert.type] ?? alert.type}</span>
              </div>
              <div className="anomaly-item__row">
                <span>{alert.message}</span>
              </div>
              <div className="anomaly-item__row anomaly-item__row--meta">
                <span>{formatRelativeTime(alert.timestamp)}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
