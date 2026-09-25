import { useCallback } from 'react'
import { getAlerts } from '../api/client'
import { useBrandFilter } from '../context/BrandFilterContext'
import { POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatRelativeTime, SEVERITY_LABEL } from '../utils/formatters'
import { DetectorBadges } from './DetectorBadges'
import { ErrorState } from './ErrorState'
import { ExplainButton } from './ExplainButton'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

const TYPE_LABEL = {
  anomaly: 'Anomaly',
  low_stock: 'Low Stock',
  decline: 'Decline',
}

// A stable identity per alert, NOT the array index: the feed re-sorts on
// every poll (severity, then detector agreement, then recency), and each
// anomaly row holds its own Explain state — with index keys, React would
// hand one alert's explanation to whichever alert lands in its old slot.
function alertKey(alert) {
  return alert.related_entity.anomaly_id ?? `${alert.type}:${alert.message}`
}

// Additive alongside AnomalyPanel (kept as-is for its detailed
// region-hour anomaly view — z-score, severity, per-event) — this is
// the consolidated, at-a-glance feed over all three signal sources
// (anomalies + HIGH-risk inventory + declining regions/products), the
// "what needs my attention right now" view. See routers/alerts.py.
export function AlertsCenter() {
  const { selectedBrand } = useBrandFilter()
  const fetchFn = useCallback(() => getAlerts({ brand: selectedBrand || undefined }), [selectedBrand])
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
          {data.map((alert) => (
            <li key={alertKey(alert)} className="anomaly-item">
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
                {alert.detectors && <DetectorBadges detectors={alert.detectors} />}
              </div>
              {alert.type === 'anomaly' && <ExplainButton anomalyId={alert.related_entity.anomaly_id} />}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
