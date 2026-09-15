import { useEffect, useState } from 'react'

// Fetches once on mount (empty dependency array below — Phase 6 scope
// stops here deliberately; Phase 7 adds the interval/refresh layer on
// top of this same hook, not a rewrite of it) and exposes the three
// states every fetch actually has: still loading, failed, or here's
// the data. A plain hook, not a data-fetching library — three
// components each needing {data, loading, error} doesn't justify one.
//
// `fetchFn` is called exactly once per mount; pass a stable function
// (e.g. defined inline in the calling component — components below
// don't have props that change, so this never needs to re-fetch).
export function useApiData(fetchFn) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false

    setLoading(true)
    setError(null)

    fetchFn()
      .then((result) => {
        if (!cancelled) setData(result)
      })
      .catch((err) => {
        if (!cancelled) setError(err)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    // Guards against setting state after this component has already
    // unmounted (e.g. the request resolves after the user navigated
    // away) — React would otherwise warn about it.
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fetch-once-on-mount is the point (Phase 6); see hook docstring
  }, [])

  return { data, loading, error }
}
