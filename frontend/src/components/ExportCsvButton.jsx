import { useState } from 'react'
import { downloadCsv } from '../api/client'

// `fetchParams` is a plain object of the endpoint's own query params
// (minutes, order, brand, ...) — this component just adds format=csv
// and triggers the browser download; it doesn't know or care what
// table it's attached to beyond `path`/`filename`.
export function ExportCsvButton({ path, params, filename }) {
  const [error, setError] = useState(null)
  const [downloading, setDownloading] = useState(false)

  async function handleClick() {
    setDownloading(true)
    setError(null)
    try {
      await downloadCsv(path, params, filename)
    } catch (err) {
      setError(err)
    } finally {
      setDownloading(false)
    }
  }

  return (
    <span className="export-csv">
      <button type="button" className="export-csv__button" onClick={handleClick} disabled={downloading}>
        {downloading ? 'Exporting…' : 'Export CSV'}
      </button>
      {error && <span className="export-csv__error">{error.message}</span>}
    </span>
  )
}
