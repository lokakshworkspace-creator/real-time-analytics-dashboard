// Thin fetch wrapper around the backend API. Plain fetch(), no axios/
// swr/react-query — per CLAUDE.md, this app's data needs (a handful of
// GET endpoints, fetched once per component) don't justify a data-
// fetching library any more than they justify Redux.

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

// Every backend route now lives under /api (Phase 8 — previously only
// POST /metrics did, the three GET endpoints didn't; standardized as
// one coordinated change with the backend, not two out-of-sync halves).
// Prepended once here, not per-call below, so every current and future
// endpoint function automatically matches whatever the backend does.
const API_PREFIX = '/api'

async function getJson(path) {
  let response
  try {
    response = await fetch(`${API_BASE_URL}${API_PREFIX}${path}`)
  } catch {
    // fetch() itself throws on network failure (backend down, CORS
    // blocked, DNS, etc.) — normalize it into the same Error type a
    // non-2xx response produces below, so every caller has one shape
    // of failure to handle instead of two.
    throw new Error(`Could not reach the API at ${API_BASE_URL}. Is the backend running?`)
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

export function getLatestMetrics(source) {
  const query = source ? `?source=${encodeURIComponent(source)}` : ''
  return getJson(`/metrics/latest${query}`)
}

export function getMetricHistory(metric, minutes = 60) {
  return getJson(`/metrics/history?metric=${encodeURIComponent(metric)}&minutes=${minutes}`)
}

export function getAnomalies(limit = 50) {
  return getJson(`/metrics/anomalies?limit=${limit}`)
}
