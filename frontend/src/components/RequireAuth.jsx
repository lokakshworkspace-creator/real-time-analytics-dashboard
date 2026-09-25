import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

// The top-level route guard: every page except /login renders inside
// this. Signed out -> /login, remembering where the user was headed
// (`state.from`) so a successful login (or one forced by an expired
// token mid-session) lands them back there instead of always on the
// landing page. Note the session lives in memory only (see
// AuthContext), so a full page reload of any URL comes back through
// here too and asks for a login again — routing doesn't change that.
export function RequireAuth() {
  const { isAuthenticated } = useAuth()
  const location = useLocation()

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }
  return <Outlet />
}
