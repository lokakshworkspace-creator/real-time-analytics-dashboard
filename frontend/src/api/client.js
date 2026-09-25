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
  return requestJson('GET', path, { params })
}

// A POST for endpoints with no request body (e.g. /anomalies/{id}/explain
// — the id in the path is the whole input).
async function postJson(path) {
  return requestJson('POST', path)
}

// `handleUnauthorized: false` opts one call out of the automatic
// "401 => the session is over, log out" behavior below, for the rare
// endpoint where a 401 means something else (see changePassword).
async function requestJson(method, path, { params, body, handleUnauthorized = true } = {}) {
  const query = params
    ? '?' +
      Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== null && value !== '')
        .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
        .join('&')
    : ''

  const headers = currentToken ? { Authorization: `Bearer ${currentToken}` } : {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response
  try {
    response = await fetch(`${API_BASE_URL}${API_PREFIX}${path}${query}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    // fetch() itself throws on network failure (backend down, CORS
    // blocked, DNS, etc.) — normalize it into the same Error type a
    // non-2xx response produces below, so every caller has one shape
    // of failure to handle instead of two.
    throw new Error(`Could not reach the API at ${API_BASE_URL}. Is the backend running?`)
  }

  if (response.status === 401 && unauthorizedHandler && handleUnauthorized) {
    unauthorizedHandler()
  }

  if (!response.ok) {
    let detail = ''
    try {
      const errorBody = await response.json()
      // FastAPI sends either a plain string (our own HTTPExceptions) or
      // a list of {msg, loc, ...} objects (request validation, 422) —
      // flatten the list to its messages so those are readable too.
      if (typeof errorBody.detail === 'string') {
        detail = errorBody.detail
      } else if (Array.isArray(errorBody.detail)) {
        detail = errorBody.detail.map((e) => e.msg).join('; ')
      }
    } catch {
      // Response wasn't JSON (e.g. a proxy error page) — fall back to
      // just the status line, still better than swallowing the error.
    }
    const error = new Error(
      `${path} failed (${response.status} ${response.statusText})${detail ? `: ${detail}` : ''}`
    )
    // The server's own message, on its own, for UI that wants to show a
    // person something readable (e.g. the explain button's "try again
    // shortly") instead of the developer-oriented line above.
    error.detail = detail
    error.status = response.status
    throw error
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

// Self-service profile edit — today only a business account's display
// name (the backend rejects anything else; see routers/auth.py).
export function updateProfile({ businessName }) {
  return requestJson('PATCH', '/auth/me', { body: { business_name: businessName } })
}

// A wrong current password comes back as 401, which every other
// endpoint uses to mean "your session expired". Here that would sign
// the user out for a typo, so this call opts out of the automatic
// logout — and hands it back only if the 401 is a genuinely bad/expired
// token rather than the wrong-password reply.
export const WRONG_CURRENT_PASSWORD_DETAIL = 'Current password is incorrect'

export async function changePassword({ currentPassword, newPassword }) {
  try {
    return await requestJson('POST', '/auth/change-password', {
      body: { current_password: currentPassword, new_password: newPassword },
      handleUnauthorized: false,
    })
  } catch (error) {
    if (error.status === 401 && error.detail !== WRONG_CURRENT_PASSWORD_DETAIL && unauthorizedHandler) {
      unauthorizedHandler()
    }
    throw error
  }
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

// On-demand LLM explanation of one flagged anomaly. POST because the
// first call spends quota and stores the result; repeat calls return the
// stored one (response.cached === true) at no cost.
export function explainAnomaly(anomalyId) {
  return postJson(`/anomalies/${encodeURIComponent(anomalyId)}/explain`)
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
export function getAlerts({ brand } = {}) {
  return getJson('/alerts', { params: { brand } })
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
