import { KpiCardsRow } from '../components/KpiCardsRow'
import { RecentOrdersTable } from '../components/RecentOrdersTable'
import { RegionsChart } from '../components/RegionsChart'
import { TrendChart } from '../components/TrendChart'

// Landing page: how the business is doing right now. Each component
// fetches and polls its own data — and only while this page is mounted.
export function OverviewPage() {
  return (
    <>
      <h1 className="page-title">Overview</h1>
      <KpiCardsRow />
      <div className="dashboard__lower dashboard__lower--even">
        <TrendChart />
        <RegionsChart />
      </div>
      <RecentOrdersTable />
    </>
  )
}
