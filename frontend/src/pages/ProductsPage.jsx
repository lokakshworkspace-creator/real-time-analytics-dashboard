import { InventoryRiskTable } from '../components/InventoryRiskTable'
import { ProductPerformanceTable } from '../components/ProductPerformanceTable'

// What's selling and what's about to run out. Both tables keep their own
// CSV export buttons (they live inside the components, not this page).
export function ProductsPage() {
  return (
    <>
      <h1 className="page-title">Products &amp; Inventory</h1>
      <ProductPerformanceTable />
      <InventoryRiskTable />
    </>
  )
}
