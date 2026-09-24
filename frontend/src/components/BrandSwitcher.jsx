import { useCallback } from 'react'
import { getBrands } from '../api/client'
import { useBrandFilter } from '../context/BrandFilterContext'
import { useApiData } from '../hooks/useApiData'

// Admin-only (see App.jsx — rendered only for role="admin"). Business
// accounts are already scoped to their own brand(s) server-side and
// have nothing to switch between.
export function BrandSwitcher() {
  const { selectedBrand, setSelectedBrand } = useBrandFilter()
  const fetchFn = useCallback(() => getBrands(), [])
  const { data: brands } = useApiData(fetchFn) // no polling — the brand list doesn't change minute to minute

  return (
    <label className="brand-switcher">
      <span>Brand</span>
      <select value={selectedBrand} onChange={(e) => setSelectedBrand(e.target.value)}>
        <option value="">All brands</option>
        {(brands ?? []).map((brand) => (
          <option key={brand} value={brand}>
            {brand}
          </option>
        ))}
      </select>
    </label>
  )
}
