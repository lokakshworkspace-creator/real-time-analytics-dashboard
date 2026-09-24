import { useCallback, useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { getRegionStats } from '../api/client'
import { useBrandFilter } from '../context/BrandFilterContext'
import { POLL_INTERVAL_MS } from '../constants'
import { DeltaBadge } from './DeltaBadge'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RangeToggle } from './RangeToggle'
import { RefreshIndicator } from './RefreshIndicator'

const TITLE = 'Revenue by Region'

export function RegionsChart() {
  const [range, setRange] = useState('7d')
  const { selectedBrand } = useBrandFilter()
  const fetchFn = useCallback(
    () => getRegionStats({ range, brand: selectedBrand || undefined }),
    [range, selectedBrand]
  )
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  // Recharts needs a flat dataKey for the bar's value — `current.revenue`
  // dotted-path access doesn't work as a plain dataKey string, so the
  // chart gets a flattened copy while the delta list below reads the
  // full nested shape directly.
  const chartData = (data ?? []).map((r) => ({ region: r.region, revenue: r.current.revenue }))

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">{TITLE}</h2>
        <div className="section-header__controls">
          <RangeToggle value={range} onChange={setRange} />
          <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
        </div>
      </div>

      {loading && <LoadingState label="Loading regional data…" />}
      {error && <ErrorState error={error} label="Could not load regional data" />}

      {!loading && !error && (!data || data.length === 0) && (
        <p className="empty-note">No orders in this range yet — run the simulator to generate some.</p>
      )}

      {!loading && !error && data && data.length > 0 && (
        <>
          <div className="chart-wrap">
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="region" stroke="var(--text-muted)" />
                <YAxis stroke="var(--text-muted)" tickFormatter={formatCurrency} width={72} />
                <Tooltip formatter={(value) => [formatCurrency(value), 'Revenue']} />
                <Bar dataKey="revenue" fill="var(--accent)" radius={[4, 4, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>Region</th>
                <th>Revenue</th>
                <th>vs. previous</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.region}>
                  <td>{r.region}</td>
                  <td>{formatCurrency(r.current.revenue)}</td>
                  <td>
                    <DeltaBadge delta={r.change_pct.revenue} />
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
