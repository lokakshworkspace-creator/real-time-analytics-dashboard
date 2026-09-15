import './App.css'
import { AnomalyPanel } from './components/AnomalyPanel'
import { MetricCardsRow } from './components/MetricCardsRow'
import { TrendChart } from './components/TrendChart'

// Three independent sections, each fetching its own data (see
// MetricCardsRow/TrendChart/AnomalyPanel) rather than App fetching
// everything and passing it down as props: each stays self-contained
// and fails independently — the anomaly panel showing its own error
// state doesn't blank out the metric cards next to it, and each one
// can grow its own Phase 7 polling interval later without the others
// needing to know about it.
function App() {
  return (
    <div className="dashboard">
      <header className="dashboard__header">
        <h1>Real-Time Data Analytics Dashboard</h1>
        <p className="dashboard__subtitle">
          Synthetic metric stream — see the simulator, not live production data.
        </p>
      </header>

      <MetricCardsRow />

      <div className="dashboard__lower">
        <TrendChart />
        <AnomalyPanel />
      </div>
    </div>
  )
}

export default App
