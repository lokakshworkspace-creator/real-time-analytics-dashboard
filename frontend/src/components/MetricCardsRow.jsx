import { useCallback } from 'react'
import { getLatestMetrics } from '../api/client'
import { useApiData } from '../hooks/useApiData'
import { METRIC_NAMES } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { MetricCard } from './MetricCard'

export function MetricCardsRow() {
  const fetchFn = useCallback(() => getLatestMetrics(), [])
  const { data, loading, error } = useApiData(fetchFn)

  if (loading) return <LoadingState label="Loading metrics…" />
  if (error) return <ErrorState error={error} label="Could not load metric cards" />

  // GET /metrics/latest returns up to 5 entries, one per metric that
  // has ever received data — look each one up by name so every one of
  // the 5 known metrics always renders a card (with an empty state for
  // any that are missing), rather than a row that's only as wide as
  // however many entries happened to come back.
  const byMetric = Object.fromEntries((data ?? []).map((entry) => [entry.metric, entry]))

  return (
    <div className="metric-cards-row">
      {METRIC_NAMES.map((metric) => (
        <MetricCard key={metric} metric={metric} entry={byMetric[metric] ?? null} />
      ))}
    </div>
  )
}
