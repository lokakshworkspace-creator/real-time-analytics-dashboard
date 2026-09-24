import { useCallback, useEffect, useState } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { getOrderForecast, getOrderTrend, getProductStats } from '../api/client'
import { useBrandFilter } from '../context/BrandFilterContext'
import { POLL_INTERVAL_MS } from '../constants'
import { useApiData } from '../hooks/useApiData'
import { formatCurrency } from '../utils/formatters'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'
import { RangeToggle } from './RangeToggle'
import { RefreshIndicator } from './RefreshIndicator'

const TITLE = 'Revenue Trend'
const FORECAST_HORIZON_DAYS = 7

// Merges real trend points with forecast points into one array Recharts
// can plot as two lines sharing an x-axis: `revenue` populated only for
// real (past) points, `projected` populated only for forecast (future)
// points, with one bridging point carrying BOTH so the dashed forecast
// line visually connects to where the solid actual line ends instead
// of leaving a gap.
function buildOverlayData(trend, forecast) {
  const points = trend.map((point) => ({ period: point.period, revenue: point.revenue }))
  if (forecast.length > 0 && points.length > 0) {
    points[points.length - 1] = {
      ...points[points.length - 1],
      projected: points[points.length - 1].revenue,
    }
  }
  for (const point of forecast) {
    points.push({ period: point.date, projected: point.projected_revenue })
  }
  return points
}

export function TrendChart() {
  const [range, setRange] = useState('7d')
  const [showForecast, setShowForecast] = useState(false)
  const [forecastProductId, setForecastProductId] = useState(null)
  const [forecastProductName, setForecastProductName] = useState(null)
  const { selectedBrand } = useBrandFilter()
  const brand = selectedBrand || undefined

  // granularity omitted deliberately — the backend already picks a
  // sensible default per range (day for 7d/30d, month for 1y, see
  // routers/orders.py's DEFAULT_GRANULARITY), and this chart doesn't
  // need its own control duplicating that choice.
  const fetchFn = useCallback(() => getOrderTrend({ range, brand }), [range, brand])
  const { data, loading, error, isRefreshing, pollError, lastUpdated } = useApiData(fetchFn, {
    intervalMs: POLL_INTERVAL_MS,
  })

  // The forecast endpoint is per-product (Feature 3 is a per-SKU
  // projection, not an overall-revenue one) — the toggle forecasts
  // whichever product currently has the most revenue in this range,
  // fetched once when the toggle turns on rather than every poll tick.
  useEffect(() => {
    if (!showForecast) {
      setForecastProductId(null)
      setForecastProductName(null)
      return
    }
    let cancelled = false
    getProductStats({ range, limit: 1, order: 'top', brand }).then((result) => {
      if (cancelled) return
      const top = result.products[0]
      setForecastProductId(top?.product_id ?? null)
      setForecastProductName(top?.product_name ?? null)
    })
    return () => {
      cancelled = true
    }
  }, [showForecast, range, brand])

  const forecastFetchFn = useCallback(
    () =>
      forecastProductId
        ? getOrderForecast({ productId: forecastProductId, horizon: FORECAST_HORIZON_DAYS })
        : Promise.resolve(null),
    [forecastProductId]
  )
  const forecastResult = useApiData(forecastFetchFn, { intervalMs: showForecast ? POLL_INTERVAL_MS : undefined })

  const overlayData = buildOverlayData(data ?? [], showForecast ? forecastResult.data?.forecast ?? [] : [])

  return (
    <section className="panel">
      <div className="section-header">
        <h2 className="panel__title">{TITLE}</h2>
        <div className="section-header__controls">
          <label className="forecast-toggle">
            <input
              type="checkbox"
              checked={showForecast}
              onChange={(e) => setShowForecast(e.target.checked)}
            />
            Forecast
          </label>
          <RangeToggle value={range} onChange={setRange} />
          <RefreshIndicator isRefreshing={isRefreshing} pollError={pollError} lastUpdated={lastUpdated} />
        </div>
      </div>

      {loading && <LoadingState label="Loading trend…" />}
      {error && <ErrorState error={error} label="Could not load trend" />}

      {!loading && !error && (!data || data.length === 0) && (
        <p className="empty-note">No orders in this range yet — run the simulator to generate some.</p>
      )}

      {!loading && !error && data && data.length > 0 && (
        <>
          <div className="chart-wrap">
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={overlayData} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="period" stroke="var(--text-muted)" minTickGap={24} />
                <YAxis stroke="var(--text-muted)" tickFormatter={formatCurrency} width={72} />
                <Tooltip
                  formatter={(value, name) => [
                    formatCurrency(value),
                    name === 'projected' ? 'Projected' : 'Revenue',
                  ]}
                />
                <Area
                  type="monotone"
                  dataKey="revenue"
                  stroke="var(--accent)"
                  strokeWidth={2}
                  fill="url(#trendFill)"
                  isAnimationActive={false}
                  connectNulls={false}
                />
                {showForecast && (
                  <Line
                    type="monotone"
                    dataKey="projected"
                    stroke="var(--text-muted)"
                    strokeWidth={2}
                    strokeDasharray="5 5"
                    dot={false}
                    isAnimationActive={false}
                    connectNulls
                  />
                )}
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <p className="panel__footnote">
            {data.length} point{data.length === 1 ? '' : 's'}
            {showForecast && forecastResult.data && (
              <>
                {' '}
                · forecasting {forecastProductName ?? '…'} ({forecastResult.data.method.replaceAll('_', ' ')})
              </>
            )}
          </p>
        </>
      )}
    </section>
  )
}
