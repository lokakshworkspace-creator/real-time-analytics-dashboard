import { useCallback } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { getRegionStats } from '../api/client'
import { DEFAULT_WINDOW_MINUTES, POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RefreshIndicator } from './RefreshIndicator'

const TITLE = `Revenue by Region — last ${DEFAULT_WINDOW_MINUTES} min`

export function RegionsChart() {
  const fetchFn = useCallback(() => getRegionStats(DEFAULT_WINDOW_MINUTES), [])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  if (loading) {
    return (
      <section className="panel">
        <h2 className="panel__title">{TITLE}</h2>
        <LoadingState label="Loading regional data…" />
      </section>
    )
  }

  if (error) {
    return (
      <section className="panel">
        <h2 className="panel__title">{TITLE}</h2>
        <ErrorState error={error} label="Could not load regional data" />
      </section>
    )
  }

  if (!data || data.length === 0) {
    return (
      <section className="panel">
        <div className="section-header">
          <h2 className="panel__title">{TITLE}</h2>
          <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
        </div>
        <p className="empty-note">No orders in this window yet — run the simulator to generate some.</p>
      </section>
    )
  }

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">{TITLE}</h2>
        <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
      </div>
      <div className="chart-wrap">
        <ResponsiveContainer width="100%" height={280}>
          <BarChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis dataKey="region" stroke="var(--text-muted)" />
            <YAxis stroke="var(--text-muted)" tickFormatter={formatCurrency} width={72} />
            <Tooltip
              formatter={(value, name) => [
                name === 'revenue' ? formatCurrency(value) : value,
                name === 'revenue' ? 'Revenue' : 'Orders',
              ]}
            />
            <Bar dataKey="revenue" fill="var(--accent)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="panel__footnote">{data.length} region{data.length === 1 ? '' : 's'}</p>
    </section>
  )
}
