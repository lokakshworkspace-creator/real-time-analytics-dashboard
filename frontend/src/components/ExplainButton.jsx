import { useEffect, useRef, useState } from 'react'
import { explainAnomaly } from '../api/client'

// "Explain" for one anomaly: click -> loading -> the LLM's short
// explanation and suggested action shown inline. On demand only —
// nothing is requested until the user clicks, and the backend caches the
// result on the anomaly record, so a repeat click costs nothing.
//
// The result carries `cached`, and the two cases look different on
// purpose: a freshly generated explanation is tagged "Just generated"
// (it spent a model call, and is worth reading with slightly more
// scrutiny), a cached one is tagged "Saved" (the identical text shown
// before, at no cost).
//
// State lives here, per row, so it survives the panels' 5-second polling
// refresh: callers must key rows by a stable id (not an array index) or
// React would hand one row's explanation to another when the list
// re-sorts.
export function ExplainButton({ anomalyId }) {
  const [state, setState] = useState({ status: 'idle' })
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  async function handleClick() {
    setState({ status: 'loading' })
    try {
      const result = await explainAnomaly(anomalyId)
      if (mounted.current) setState({ status: 'done', result })
    } catch (error) {
      if (mounted.current) {
        setState({
          status: 'error',
          message: error.detail || 'Could not get an explanation. Please try again.',
        })
      }
    }
  }

  if (state.status === 'done') {
    const { result } = state
    return (
      <div className="explanation">
        <div className="explanation__header">
          <span className="explanation__label">Explanation</span>
          <span
            className={`explanation__tag explanation__tag--${result.cached ? 'cached' : 'fresh'}`}
            title={
              result.cached
                ? 'Loaded from the saved explanation — no model call was made'
                : 'Generated just now by the model'
            }
          >
            {result.cached ? 'Saved' : 'Just generated'}
          </span>
        </div>
        <p className="explanation__text">{result.explanation}</p>
        <p className="explanation__action">
          <strong>Suggested action:</strong> {result.suggested_action}
        </p>
      </div>
    )
  }

  return (
    <div className="explain">
      <button
        type="button"
        className="explain__button"
        onClick={handleClick}
        disabled={state.status === 'loading'}
      >
        {state.status === 'loading' ? 'Explaining…' : 'Explain'}
      </button>
      {state.status === 'error' && <span className="explain__error">{state.message}</span>}
    </div>
  )
}
