import { NavLink } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { BrandSwitcher } from './BrandSwitcher'

// Every page, in nav order. `adminOnly` links are hidden from business
// accounts here — but that is cosmetic; the route itself is guarded
// separately (see RequireAdmin) because a hidden link protects nothing.
const NAV_ITEMS = [
  { to: '/overview', label: 'Overview' },
  { to: '/products', label: 'Products' },
  { to: '/anomalies', label: 'Anomalies' },
  { to: '/benchmark', label: 'Benchmark', adminOnly: true },
  { to: '/profile', label: 'Profile' },
]

export function Navbar() {
  const { user, logout } = useAuth()
  const isAdmin = user.role === 'admin'

  // A deliberate sign-out: RequireAuth sends the user to /login, and
  // forgetLocation makes the next login start at the landing page
  // instead of resuming this one (right for an expired session, wrong
  // for someone who chose to leave).
  function handleLogout() {
    logout({ forgetLocation: true })
  }

  return (
    <header className="navbar">
      <div className="navbar__inner">
        <span className="navbar__brand">E-Commerce Analytics</span>

        <nav className="navbar__links" aria-label="Main">
          {NAV_ITEMS.filter((item) => !item.adminOnly || isAdmin).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `navbar__link${isActive ? ' navbar__link--active' : ''}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="navbar__controls">
          {isAdmin ? (
            <BrandSwitcher />
          ) : (
            <span className="viewing-badge">Viewing: {user.business_name ?? user.owned_brands.join(', ')}</span>
          )}
          <button type="button" className="logout-button" onClick={handleLogout}>
            Sign out
          </button>
        </div>
      </div>
    </header>
  )
}
