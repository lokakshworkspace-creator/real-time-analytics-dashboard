import './App.css'
import { AlertsCenter } from './components/AlertsCenter'
import { AnomalyPanel } from './components/AnomalyPanel'
import { BenchmarkTable } from './components/BenchmarkTable'
import { BrandSwitcher } from './components/BrandSwitcher'
import { InventoryRiskTable } from './components/InventoryRiskTable'
import { KpiCardsRow } from './components/KpiCardsRow'
import { LoginPage } from './components/LoginPage'
import { ProductPerformanceTable } from './components/ProductPerformanceTable'
import { RecentOrdersTable } from './components/RecentOrdersTable'
import { RegionsChart } from './components/RegionsChart'
import { TrendChart } from './components/TrendChart'
import { AuthProvider, useAuth } from './context/AuthContext'
import { BrandFilterProvider } from './context/BrandFilterContext'

// Each dashboard section fetches its own data (see KpiCardsRow/
// RegionsChart/TrendChart/ProductPerformanceTable/InventoryRiskTable/
// RecentOrdersTable/AnomalyPanel) rather than App fetching everything
// and passing it down as props: each stays self-contained and fails
// independently — one section's error state doesn't blank out the
// sections next to it, and each one owns its own polling interval
// without the others needing to know about it. BrandFilterProvider
// wraps them all so picking a brand in BrandSwitcher re-scopes every
// section at once (see BrandFilterContext).
function Dashboard() {
  const { user, logout } = useAuth()
  const isAdmin = user.role === 'admin'

  return (
    <BrandFilterProvider>
      <div className="dashboard">
        <header className="dashboard__header">
          <div className="dashboard__header-row">
            <div>
              <h1>Real-Time E-Commerce Analytics Dashboard</h1>
              <p className="dashboard__subtitle">
                Synthetic order stream — see the simulator, not live production data.
              </p>
            </div>
            <div className="dashboard__header-controls">
              {isAdmin ? (
                <BrandSwitcher />
              ) : (
                <span className="viewing-badge">Viewing: {user.business_name ?? user.owned_brands.join(', ')}</span>
              )}
              <button type="button" className="logout-button" onClick={logout}>
                Sign out
              </button>
            </div>
          </div>
        </header>

        <KpiCardsRow />

        <div className="dashboard__lower">
          <TrendChart />
          <AnomalyPanel />
        </div>

        <div className="dashboard__lower">
          <RegionsChart />
          <ProductPerformanceTable />
        </div>

        <div className="dashboard__lower">
          <RecentOrdersTable />
          <InventoryRiskTable />
        </div>

        {/* Additive alongside AnomalyPanel, not a replacement — see
            AlertsCenter.jsx for why both stay: AnomalyPanel is the
            detailed per-event z-score view, AlertsCenter is the
            consolidated "what needs attention" feed across all three
            signal sources (anomalies + low stock + declines).
            BenchmarkTable only renders for admins (backend also 403s a
            business account, but this avoids even attempting the call
            and keeps a business user's layout single-column instead of
            leaving a visibly empty second column next to it). */}
        {isAdmin ? (
          <div className="dashboard__lower">
            <AlertsCenter />
            <BenchmarkTable />
          </div>
        ) : (
          <AlertsCenter />
        )}
      </div>
    </BrandFilterProvider>
  )
}

function AuthGate() {
  const { isAuthenticated } = useAuth()
  return isAuthenticated ? <Dashboard /> : <LoginPage />
}

function App() {
  return (
    <AuthProvider>
      <AuthGate />
    </AuthProvider>
  )
}

export default App
