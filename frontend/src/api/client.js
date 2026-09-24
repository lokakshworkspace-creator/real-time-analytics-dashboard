// Thin fetch wrapper around the backend API. Plain fetch(), no axios/
// swr/react-query — per CLAUDE.md, this app's data needs (a handful of
// GET endpoints, fetched once per component) don't justify a data-
// fetching library any more than they justify Redux.

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

// Every backend route lives under /api. Prepended once here, not per-
// call below, so every current and future endpoint function
// automatically matches whatever the backend does.
const API_PREFIX = '/api'

// The current JWT, held in a module-level variable rather than passed
// into every call — every authenticated fetch in this file reads it,
// and there's exactly one at a time (this app never juggles multiple
// logged-in sessions in one tab). AuthContext is the only thing that
// calls setAuthToken, on login/logout — see its own file for why the
// token itself lives in React state, not localStorage, and this
// module-level copy is just a mirror `getJson` can read synchronously
// without needing to be a hook itself.
let currentToken = null

export function setAuthToken(token) {
  currentToken = token
}

// Called once, by AuthContext, so a 401 from ANY endpoint (token
// expired, revoked, or simply never set) triggers one consistent
// "log the user out and show the login screen" response — every
// component that fetches data doesn't need its own 401-handling logic.
let unauthorizedHandler = null

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler
}

async function getJson(path, { params } = {}) {
  const query = params
    ? '?' +
      Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== null && value !== '')
        .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
        .join('&')
    : ''

  const headers = currentToken ? { Authorization: `Bearer ${currentToken}` } : {}

  let response
  try {
    response = await fetch(`${API_BASE_URL}${API_PREFIX}${path}${query}`, { headers })
  } catch {
    // fetch() itself throws on network failure (backend down, CORS
    // blocked, DNS, etc.) — normalize it into the same Error type a
    // non-2xx response produces below, so every caller has one shape
    // of failure to handle instead of two.
    throw new Error(`Could not reach the API at ${API_BASE_URL}. Is the backend running?`)
  }

  if (response.status === 401 && unauthorizedHandler) {
    unauthorizedHandler()
  }

  if (!response.ok) {
    let detail = ''
    try {
      const body = await response.json()
      detail = body.detail ? `: ${body.detail}` : ''
    } catch {
      // Response wasn't JSON (e.g. a proxy error page) — fall back to
      // just the status line, still better than swallowing the error.
    }
    throw new Error(`${path} failed (${response.status} ${response.statusText})${detail}`)
  }

  return response.json()
}

// --- Auth ------------------------------------------------------------

export async function login(email, password) {
  const response = await fetch(`${API_BASE_URL}${API_PREFIX}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!response.ok) {
    let detail = 'Incorrect email or password'
    try {
      const body = await response.json()
      detail = body.detail ?? detail
    } catch {
      // non-JSON error body — keep the generic message
    }
    throw new Error(detail)
  }
  return response.json() // { access_token, token_type, expires_in }
}

export function getMe() {
  return getJson('/auth/me')
}

// --- Orders / KPIs / trend ---------------------------------------------

export function getOrders({ limit = 50, skip = 0, brand } = {}) {
  return getJson('/orders', { params: { limit, skip, brand } })
}

export function getOrderKpis({ range = '7d', brand } = {}) {
  return getJson('/orders/kpis', { params: { range, brand } })
}

export function getRegionStats({ range = '7d', brand } = {}) {
  return getJson('/orders/regions', { params: { range, brand } })
}

export function getProductStats({ range = '7d', limit = 10, order = 'top', brand } = {}) {
  return getJson('/orders/products', { params: { range, limit, order, brand } })
}

export function getOrderTrend({ range = '7d', granularity, brand } = {}) {
  return getJson('/orders/trend', { params: { range, granularity, brand } })
}

// Feature 3: simple demand forecast — a dashed-line overlay on
// TrendChart, not its own section (see TrendChart.jsx).
export function getOrderForecast({ productId, horizon = 7 }) {
  return getJson('/orders/forecast', { params: { product_id: productId, horizon } })
}

export function getInventory({ brand } = {}) {
  return getJson('/inventory', { params: { brand } })
}

export function getInventoryRisk({ minutes = 1440, lowStockThreshold = 20, brand } = {}) {
  return getJson('/inventory/risk', {
    params: { minutes, low_stock_threshold: lowStockThreshold, brand },
  })
}

export function getBusinessAnomalies({ limit = 50, brand } = {}) {
  return getJson('/anomalies/business', { params: { limit, brand } })
}

export function getBrands() {
  return getJson('/brands')
}

// Feature 5: admin-only cross-brand benchmarking — 403s for a business
// account, so BenchmarkTable only ever renders for role="admin"
// (see App.jsx) rather than relying on this call failing gracefully.
export function getBrandsBenchmark({ range = '30d' } = {}) {
  return getJson('/brands/benchmark', { params: { range } })
}

// Feature 4: consolidated anomaly + low-stock + decline feed.
export function getAlerts() {
  return getJson('/alerts')
}

// --- CSV export --------------------------------------------------------

// Building a plain <a href="...?format=csv"> wouldn't carry the
// Authorization header a real browser navigation can't attach, and
// every export endpoint requires one — so exporting means fetching the
// CSV as a blob ourselves and handing the browser a throwaway object
// URL to download from, exactly what a real `<a download>` click would
// do with a same-origin, unauthenticated file.
export async function downloadCsv(path, params, filename) {
  const query = Object.entries({ ...params, format: 'csv' })
    .filter(([, value]) => value !== undefined && value !== null && value !== '')
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
    .join('&')

  const headers = currentToken ? { Authorization: `Bearer ${currentToken}` } : {}
  const response = await fetch(`${API_BASE_URL}${API_PREFIX}${path}?${query}`, { headers })
  if (!response.ok) {
    throw new Error(`Export failed (${response.status} ${response.statusText})`)
  }

  const blob = await response.blob()
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(objectUrl)
}
