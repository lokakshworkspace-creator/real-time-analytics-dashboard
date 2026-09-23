import { useCallback } from 'react'
import { getBusinessAnomalies } from '../api/client'
import { POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency, formatInteger, formatRelativeTime } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

export function AnomalyPanel() {
  const fetchFn = useCallback(() => getBusinessAnomalies(50), [])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Order Volume Anomalies</h2>
        <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
      </div>

      {loading && <LoadingState label="Loading anomalies…" />}
      {error && <ErrorState error={error} label="Could not load anomalies" />}

      {!loading && !error && (data === null || data.length === 0) && (
        <p className="empty-note">
          No unusual order volume yet — flags a region's hourly order count against its recent
          history (see README).
        </p>
      )}

      {!loading && !error && data && data.length > 0 && (
        <ul className="anomaly-list">
          {data.map((event) => (
            <li key={event.id} className="anomaly-item">
              <div className="anomaly-item__row">
                <span className="anomaly-item__metric">{event.region}</span>
                <span className="anomaly-item__value">{formatCurrency(event.total_value)}</span>
              </div>
              <div className="anomaly-item__row anomaly-item__row--meta">
                <span>{event.product_name}</span>
                <span>qty {formatInteger(event.quantity)}</span>
                <span>{formatRelativeTime(event.timestamp)}</span>
                <span className="anomaly-item__z">
                  z = {event.z_score !== null ? event.z_score.toFixed(2) : '—'}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
