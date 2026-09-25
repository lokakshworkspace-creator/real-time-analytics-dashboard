import { DETECTORS } from '../utils/detectors'

// A small row showing WHICH detectors agreed on an anomaly, plus how
// many ("2 of 3"). Only detectors that flagged it get a badge — a
// detector that didn't run yet (no batch pass has scored the record) and
// one that ran and passed it both simply don't appear, since either way
// it isn't evidence for the flag.
export function DetectorBadges({ detectors }) {
  return (
    <span className="detector-badges" aria-label={`Flagged by ${detectors.length} of 3 detectors`}>
      {DETECTORS.filter((d) => detectors.includes(d.key)).map((d) => (
        <span key={d.key} className="detector-badge" title={d.title}>
          {d.label}
        </span>
      ))}
      <span className="detector-badges__count">
        {detectors.length} of {DETECTORS.length}
      </span>
    </span>
  )
}
