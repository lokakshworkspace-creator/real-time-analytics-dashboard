import { useCallback } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { getMetricHistory } from '../api/client'
import { POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatClockTime, formatMetricValue, METRIC_DISPLAY } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

// One metric charted in detail, per CLAUDE.md's Phase 6 scope ("pick
// ONE metric... e.g. cpu_usage"). cpu_usage: continuous, fluctuates
// every tick with no big gaps at 0 (unlike orders' occasional
// zero-floor spikes or failed_requests' low-integer noise), which
// makes it the most legible single line to read at a glance.
const CHARTED_METRIC = 'cpu_usage'
const HISTORY_WINDOW_MINUTES = 60

export function TrendChart() {
  const fetchFn = useCallback(() => getMetricHistory(CHARTED_METRIC, HISTORY_WINDOW_MINUTES), [])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  const { label, unit } = METRIC_DISPLAY[CHARTED_METRIC]
  const title = `${label} — last ${HISTORY_WINDOW_MINUTES} min`

  // Only reachable before the first successful fetch ever completes —
  // once `data` exists, later poll failures never reach here (see
  // useApiData's hadDataBefore split), so the chart itself is never
  // replaced by an error screen once it's had real data to show.
  if (loading) {
    return (
      <section className="panel">
        <h2 className="panel__title">{title}</h2>
        <LoadingState label="Loading history…" />
      </section>
    )
  }

  if (error) {
    return (
      <section className="panel">
        <h2 className="panel__title">{title}</h2>
        <ErrorState error={error} label="Could not load trend history" />
      </section>
    )
  }

  if (!data || data.length === 0) {
    return (
      <section className="panel">
        <div className="section-header">
          <h2 className="panel__title">{title}</h2>
          <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
        </div>
        <p className="empty-note">
          No {label.toLowerCase()} data in the last {HISTORY_WINDOW_MINUTES} minutes yet — run the
          simulator to generate some.
        </p>
      </section>
    )
  }

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">{title}</h2>
        <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
      </div>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis
              dataKey="timestamp"
              tickFormatter={formatClockTime}
              stroke="var(--text-muted)"
              minTickGap={40}
            />
            <YAxis
              stroke="var(--text-muted)"
              tickFormatter={(v) => formatMetricValue(CHARTED_METRIC, v)}
              width={56}
            />
            <Tooltip
              labelFormatter={formatClockTime}
              formatter={(value) => [formatMetricValue(CHARTED_METRIC, value), label]}
            />
            <Line
              type="monotone"
              dataKey="value"
              stroke="var(--accent)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="panel__footnote">
        {data.length} point{data.length === 1 ? '' : 's'}
        {unit ? ` · ${unit}` : ''}
      </p>
    </section>
  )
}
