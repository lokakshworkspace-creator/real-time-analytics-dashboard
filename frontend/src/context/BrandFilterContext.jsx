import { createContext, useContext, useMemo, useState } from 'react'

const BrandFilterContext = createContext(null)

// The admin's currently-selected brand (or '' for "All brands"), shared
// by every data-fetching section so picking a brand in one place
// (BrandSwitcher) re-scopes the whole dashboard at once. Wraps the
// dashboard regardless of role: a business account has no switcher to
// change it, so selectedBrand just stays '' for that session — and the
// backend ignores the `brand` param entirely for role="business"
// anyway (always scoped to owned_brands — see security.py's
// brand_match_stage), so passing it through unconditionally is
// harmless rather than something every consumer needs to special-case.
export function BrandFilterProvider({ children }) {
  const [selectedBrand, setSelectedBrand] = useState('')

  const value = useMemo(() => ({ selectedBrand, setSelectedBrand }), [selectedBrand])

  return <BrandFilterContext.Provider value={value}>{children}</BrandFilterContext.Provider>
}

export function useBrandFilter() {
  const context = useContext(BrandFilterContext)
  if (context === null) {
    throw new Error('useBrandFilter must be used within a BrandFilterProvider')
  }
  return context
}
