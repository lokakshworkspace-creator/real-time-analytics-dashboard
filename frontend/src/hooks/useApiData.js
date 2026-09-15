import { useEffect, useRef, useState } from 'react'

// Fetches on mount, and — if `intervalMs` is given — re-fetches on that
// interval afterward. This is Phase 6's hook extended, not replaced:
// call it with no second argument and it behaves identically to Phase 6
// (fetch once, {data, loading, error}, nothing else). The interval is
// additive; every Phase 6 caller keeps working unchanged.
//
// The one rule this hook exists to enforce correctly: a poll tick must
// never blank data that's already on screen. Whether a given fetch
// should show the *blocking* loading state or the *non-disruptive*
// refreshing state is decided by whether we have ever successfully
// gotten data before (`hasDataRef`), not by whether this happens to be
// the first call — so a poll retry after an initial failure still shows
// the full loading state (there's nothing to preserve), while every
// poll after a real success only ever shows the small "Refreshing…"
// indicator and leaves `data` untouched until the new result arrives.
//
// `fetchFn` is called on mount and every interval tick; pass a stable
// function (e.g. wrapped in useCallback with an empty dep array in the
// calling component, as every component below does).
export function useApiData(fetchFn, { intervalMs } = {}) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [pollError, setPollError] = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)

  // Refs, not state: read inside the effect without needing to be
  // effect dependencies (that would tear down and rebuild the interval
  // on every fetch, which is exactly the bug this hook needs to avoid).
  const fetchFnRef = useRef(fetchFn)
  fetchFnRef.current = fetchFn
  const hasDataRef = useRef(false)
  const isFetchingRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    hasDataRef.current = false

    async function runFetch() {
      // Skip this tick rather than stack a second request on top of one
      // still in flight — matters if a fetch ever takes longer than the
      // poll interval (slow network, backend under load).
      if (isFetchingRef.current) return
      isFetchingRef.current = true

      const hadDataBefore = hasDataRef.current
      if (hadDataBefore) {
        setIsRefreshing(true)
      } else {
        setLoading(true)
        setError(null)
      }

      try {
        const result = await fetchFnRef.current()
        if (cancelled) return
        hasDataRef.current = true
        setData(result)
        setLastUpdated(new Date())
        setError(null)
        setPollError(null)
      } catch (err) {
        if (cancelled) return
        // Data we already have stays exactly as it is — only the error
        // slot appropriate to our current state gets set. A failed
        // retry before we've ever had data is a blocking error (there's
        // nothing to show instead); a failed poll after real data
        // exists is a non-blocking "trouble refreshing" note.
        if (hadDataBefore) {
          setPollError(err)
        } else {
          setError(err)
        }
      } finally {
        isFetchingRef.current = false
        // No early return here on purpose (oxlint flags `return` inside
        // `finally` — it can mask control flow from try/catch, even
        // though nothing here does): guard with the condition instead.
        if (!cancelled) {
          if (hadDataBefore) {
            setIsRefreshing(false)
          } else {
            setLoading(false)
          }
        }
      }
    }

    runFetch()

    let intervalId = null
    if (intervalMs) {
      intervalId = setInterval(runFetch, intervalMs)
    }

    // Runs on unmount (and if intervalMs ever changed, before the
    // effect re-runs — it doesn't in this app's usage, but the cleanup
    // is correct either way). Missing this is the classic React bug
    // CLAUDE.md calls out by name: the interval keeps firing, tries to
    // setState on an unmounted component, and leaks. `cancelled` guards
    // the async gap (a fetch in flight when unmount happens); clearing
    // the interval stops any future tick from starting a new one at all.
    return () => {
      cancelled = true
      if (intervalId !== null) clearInterval(intervalId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fetchFn is read via fetchFnRef; intervalMs is the only real dependency
  }, [intervalMs])

  return { data, loading, error, isRefreshing, pollError, lastUpdated }
}
