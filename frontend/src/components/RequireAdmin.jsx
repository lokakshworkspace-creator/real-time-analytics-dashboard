import { Link, Outlet } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

// A second, route-level guard for admin-only pages (currently just
// /benchmark). Hiding the navbar link is not protection — anyone can
// type the URL — and the API's own 403 only stops the data, not the
// page from rendering an empty shell that looks broken. So the route
// itself refuses: a non-admin who lands here (typed URL, bookmark, a
// post-login redirect) gets a plain "not available" page, and the
// page component — and therefore its API call — never mounts.
//
// A clear in-place message rather than a silent redirect on purpose: a
// business user who followed a link deserves to be told why nothing
// showed up, not be bounced somewhere unexplained.
export function RequireAdmin() {
  const { user } = useAuth()

  if (user.role !== 'admin') {
    return (
      <section className="panel not-available" role="alert">
        <h1 className="page-title">Not available</h1>
        <p>This page is only available to administrator accounts.</p>
        <Link to="/overview">Back to Overview</Link>
      </section>
    )
  }
  return <Outlet />
}
