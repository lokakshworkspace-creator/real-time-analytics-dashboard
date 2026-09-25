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
// `fetchFn` is called on mount, on every interval tick, and immediately
// whenever it CHANGES. Callers wrap it in useCallback keyed on whatever
// the query depends on (the selected brand, the range toggle), so
// "the query changed" and "fetchFn's identity changed" are the same
// event: changing a filter re-fetches at once, shows the loading state
// (the data on screen belongs to the OLD filter and shouldn't pass as the
// new one), and restarts the poll clock. Pass a memoized function — an
// inline arrow would look "changed" on every render and refetch forever.
//
// Before this, only `intervalMs` re-ran the effect, so a filter change
// waited for the next poll tick (up to 5s, measured: 3.2s for the brand
// switcher, 5.0s for a range toggle). Every panel polls on its own
// offset timer, so after picking a brand they caught up at different
// moments — and each showed the previous brand's numbers under the new
// brand's label until its turn — which read as "the filter only works on
// some panels".
export function useApiData(fetchFn, { intervalMs } = {}) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [pollError, setPollError] = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)

  // A ref, not state: read inside the effect without being an effect
  // dependency (that would tear down and rebuild the interval on every
  // fetch, which is exactly the bug this hook needs to avoid).
  const hasDataRef = useRef(false)

  useEffect(() => {
    let cancelled = false
    // Local to THIS effect run, deliberately not a ref shared across
    // runs. React's dev-mode StrictMode mounts, cleans up, and re-mounts
    // every effect: with a shared "fetch in flight" ref, the first run's
    // fetch (already cancelled by that cleanup, its result discarded)
    // left the flag set, so the re-run skipped its own fetch and a hook
    // with no polling interval — BrandSwitcher — never loaded anything
    // in dev. Polling callers hid it (the next tick fetched). A per-run
    // flag means each run guards only its own overlapping ticks.
    let isFetching = false
    hasDataRef.current = false

    async function runFetch() {
      // Skip this tick rather than stack a second request on top of one
      // still in flight — matters if a fetch ever takes longer than the
      // poll interval (slow network, backend under load).
      if (isFetching) return
      isFetching = true

      const hadDataBefore = hasDataRef.current
      if (hadDataBefore) {
        setIsRefreshing(true)
      } else {
        setLoading(true)
        setError(null)
      }

      try {
        const result = await fetchFn()
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
        isFetching = false
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
    // fetchFn is a dependency: a changed query re-runs this effect, so
    // cleanup cancels the old fetch and clears the old interval, and the
    // new run (which closes over the new fetchFn) fetches straight away.
  }, [intervalMs, fetchFn])

  return { data, loading, error, isRefreshing, pollError, lastUpdated }
}
