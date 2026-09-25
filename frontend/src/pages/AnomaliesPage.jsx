import { AlertsCenter } from '../components/AlertsCenter'
import { AnomalyPanel } from '../components/AnomalyPanel'

// Both stay, side by side, on purpose (not merged into one feed):
// AnomalyPanel is the detailed per-order view of what the detectors
// flagged and how; AlertsCenter is the consolidated, ranked "what needs
// attention" feed across anomalies, low stock and declines.
export function AnomaliesPage() {
  return (
    <>
      <h1 className="page-title">Anomalies &amp; Alerts</h1>
      <div className="dashboard__lower dashboard__lower--even">
        <AnomalyPanel />
        <AlertsCenter />
      </div>
    </>
  )
}
