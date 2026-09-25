// The three anomaly detectors, in the order the backend lists them
// (detectors/zscore.py, detectors/isolation_forest.py,
// detectors/forecast_deviation.py). `label` is what the badge shows,
// `title` is the hover explanation of what that detector actually checks.
export const DETECTORS = [
  {
    key: 'z_score',
    label: 'Z-score',
    title: "Z-score: this region's order count this hour vs. its last 24 hours (scored when the order arrives)",
  },
  {
    key: 'isolation_forest',
    label: 'Iso Forest',
    title: 'Isolation Forest: order count, revenue and order value together vs. the last 7 days (batch run)',
  },
  {
    key: 'forecast',
    label: 'Forecast',
    title: "Forecast deviation: order count vs. the trend fitted on this region and brand's earlier hours (batch run)",
  },
]

// Which detectors flagged an /api/anomalies/business record. A detector
// whose verdict is null (no batch run has scored the record) simply
// isn't in the list — absence of a verdict is not agreement.
export function detectorsFromEvent(event) {
  const flagged = []
  if (event.z_score?.flagged) flagged.push('z_score')
  if (event.isolation_forest?.flagged) flagged.push('isolation_forest')
  if (event.forecast?.flagged) flagged.push('forecast')
  return flagged
}
