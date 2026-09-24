const RANGES = [
  { value: '7d', label: '7D' },
  { value: '30d', label: '30D' },
  { value: '1y', label: '1Y' },
]

// Shared by KpiCardsRow and TrendChart, but each owns its own range
// state independently (a prop passed in, not read from shared context)
// — consistent with this app's existing "every section is self-
// contained" pattern (see App.jsx) rather than introducing a new
// shared-state mechanism just for these two sections to stay in sync.
export function RangeToggle({ value, onChange }) {
  return (
    <div className="range-toggle" role="group" aria-label="Time range">
      {RANGES.map((range) => (
        <button
          key={range.value}
          type="button"
          className={`range-toggle__button${value === range.value ? ' range-toggle__button--active' : ''}`}
          onClick={() => onChange(range.value)}
        >
          {range.label}
        </button>
      ))}
    </div>
  )
}
