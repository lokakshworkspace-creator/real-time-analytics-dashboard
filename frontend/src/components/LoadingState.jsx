// Shared "still loading" placeholder — used by every fetching
// component so a loading dashboard always looks like one thing, not
// three different ad-hoc spinners.
export function LoadingState({ label = 'Loading…' }) {
  return (
    <div className="status-state status-state--loading" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}
