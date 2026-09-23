import './App.css'
import { AnomalyPanel } from './components/AnomalyPanel'
import { InventoryRiskTable } from './components/InventoryRiskTable'
import { KpiCardsRow } from './components/KpiCardsRow'
import { ProductPerformanceTable } from './components/ProductPerformanceTable'
import { RegionsChart } from './components/RegionsChart'

// Each section fetches its own data (see KpiCardsRow/RegionsChart/
// ProductPerformanceTable/InventoryRiskTable/AnomalyPanel) rather than
// App fetching everything and passing it down as props: each stays
// self-contained and fails independently — one section's error state
// doesn't blank out the sections next to it, and each one owns its own
// polling interval without the others needing to know about it.
function App() {
  return (
    <div className="dashboard">
      <header className="dashboard__header">
        <h1>Real-Time E-Commerce Analytics Dashboard</h1>
        <p className="dashboard__subtitle">
          Synthetic order stream — see the simulator, not live production data.
        </p>
      </header>

      <KpiCardsRow />

      <div className="dashboard__lower">
        <RegionsChart />
        <AnomalyPanel />
      </div>

      <div className="dashboard__lower">
        <ProductPerformanceTable />
        <InventoryRiskTable />
      </div>
    </div>
  )
}

export default App
