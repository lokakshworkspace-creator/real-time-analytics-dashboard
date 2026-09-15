// Shared "this fetch failed" state. Deliberately visible and specific
// (shows the actual error message from api/client.js, e.g. "Could not
// reach the API — is the backend running?") rather than a blank
// section or a silently-empty list, per Phase 6's explicit requirement
// that a broken backend must never look indistinguishable from "no
// data yet".
export function ErrorState({ error, label = 'Could not load data' }) {
  return (
    <div className="status-state status-state--error" role="alert">
      <strong>{label}</strong>
      <span>{error?.message ?? 'Unknown error'}</span>
    </div>
  )
}
