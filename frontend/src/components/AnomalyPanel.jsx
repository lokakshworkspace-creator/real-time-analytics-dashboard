import { useCallback } from 'react'
import { getAnomalies } from '../api/client'
import { POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatMetricValue, formatRelativeTime, METRIC_DISPLAY } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

export function AnomalyPanel() {
  const fetchFn = useCallback(() => getAnomalies(50), [])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">Anomalies</h2>
        <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
      </div>

      {loading && <LoadingState label="Loading anomalies…" />}
      {error && <ErrorState error={error} label="Could not load anomalies" />}

      {!loading && !error && (data === null || data.length === 0) && (
        <p className="empty-note">No anomalies yet.</p>
      )}

      {!loading && !error && data && data.length > 0 && (
        <ul className="anomaly-list">
          {data.map((event) => (
            <li key={event.id} className="anomaly-item">
              <div className="anomaly-item__row">
                <span className="anomaly-item__metric">
                  {METRIC_DISPLAY[event.metric]?.label ?? event.metric}
                </span>
                <span className="anomaly-item__value">
                  {formatMetricValue(event.metric, event.value)}
                </span>
              </div>
              <div className="anomaly-item__row anomaly-item__row--meta">
                <span>{event.source}</span>
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
