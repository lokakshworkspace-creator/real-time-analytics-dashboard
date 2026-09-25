import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const DEFAULT_LANDING = '/overview'

export function LoginPage() {
  const { login, isAuthenticated, loginNotice, forgetLocation } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  // Where the user was headed when RequireAuth sent them here (a typed
  // URL, or a session that expired mid-visit) — never back to /login itself.
  const from = location.state?.from
  const resume = from && from.pathname !== '/login' && !forgetLocation
  const destination = resume ? `${from.pathname}${from.search ?? ''}` : DEFAULT_LANDING
  // Why the user is here, if there's something to tell them (a password
  // change, an expired session) — see AuthContext's logoutInfo.
  const notice = loginNotice
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await login(email, password)
      navigate(destination, { replace: true })
    } catch (err) {
      setError(err)
    } finally {
      setSubmitting(false)
    }
  }

  // Already signed in and typed /login: nothing to do here.
  if (isAuthenticated && !submitting) {
    return <Navigate to={destination} replace />
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1 className="login-card__title">Sign in</h1>
        <p className="login-card__subtitle">Real-Time E-Commerce Analytics Dashboard</p>

        <label className="login-field">
          <span>Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
          />
        </label>

        <label className="login-field">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {notice && (
          <div className="status-state status-state--success" role="status">
            <span>{notice}</span>
          </div>
        )}

        {error && (
          <div className="status-state status-state--error" role="alert">
            <span>{error.message}</span>
          </div>
        )}

        <button type="submit" className="login-submit" disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>

        <p className="login-card__note">
          Session held in memory only — refreshing the page signs you out. See the README's
          auth section for why.
        </p>
      </form>
    </div>
  )
}
