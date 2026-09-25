import { BenchmarkTable } from '../components/BenchmarkTable'

// Admin only. Reached only through RequireAdmin (see App.jsx), so this
// component — and its request to /api/brands/benchmark — never mounts
// for a business account.
export function BenchmarkPage() {
  return (
    <>
      <h1 className="page-title">Cross-Brand Benchmark</h1>
      <BenchmarkTable />
    </>
  )
}
