import { useState } from 'react'
import { changePassword, updateProfile } from '../api/client'
import { useAuth } from '../context/AuthContext'

const ROLE_LABEL = { admin: 'Administrator', business: 'Business' }

// The same limits the backend enforces (models.py: ProfileUpdateIn,
// PASSWORD_MIN_LENGTH / PASSWORD_MAX_LENGTH). Checked here first only so
// a typo gets an instant message; the server remains the authority.
const BUSINESS_NAME_MAX = 100
const PASSWORD_MIN = 8
const PASSWORD_MAX = 72

function BusinessNameForm() {
  const { user, updateUser } = useAuth()
  const [name, setName] = useState(user.business_name ?? '')
  const [status, setStatus] = useState({ kind: 'idle' }) // idle | saving | success | error

  const trimmed = name.trim()
  const unchanged = trimmed === user.business_name
  const tooLong = trimmed.length > BUSINESS_NAME_MAX

  async function handleSubmit(event) {
    event.preventDefault()
    setStatus({ kind: 'saving' })
    try {
      const updated = await updateProfile({ businessName: trimmed })
      updateUser(updated) // the navbar's "Viewing: …" badge updates with it
      setName(updated.business_name)
      setStatus({ kind: 'success' })
    } catch (error) {
      setStatus({ kind: 'error', message: error.detail || 'Could not save the business name. Please try again.' })
    }
  }

  return (
    <section className="panel profile-section">
      <h2 className="panel__title">Business name</h2>
      <form className="form" onSubmit={handleSubmit}>
        <label className="form-field">
          <span>Name shown for your account</span>
          <input
            type="text"
            value={name}
            onChange={(e) => {
              setName(e.target.value)
              if (status.kind !== 'saving') setStatus({ kind: 'idle' })
            }}
            maxLength={BUSINESS_NAME_MAX + 20}
            autoComplete="organization"
            required
          />
        </label>
        {tooLong && <p className="form-hint form-hint--error">At most {BUSINESS_NAME_MAX} characters.</p>}
        <div className="form-actions">
          <button
            type="submit"
            className="form-submit"
            disabled={status.kind === 'saving' || trimmed.length === 0 || tooLong || unchanged}
          >
            {status.kind === 'saving' ? 'Saving…' : 'Save name'}
          </button>
          {status.kind === 'success' && (
            <span className="form-message form-message--success" role="status">
              Business name updated.
            </span>
          )}
          {status.kind === 'error' && (
            <span className="form-message form-message--error" role="alert">
              {status.message}
            </span>
          )}
        </div>
      </form>
    </section>
  )
}

function ChangePasswordForm() {
  const { logout } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [status, setStatus] = useState({ kind: 'idle' }) // idle | saving | error

  // First problem found, in the order a person would fix them; null = OK to submit.
  function validate() {
    if (next.length < PASSWORD_MIN) return `The new password must be at least ${PASSWORD_MIN} characters.`
    if (next.length > PASSWORD_MAX) return `The new password must be at most ${PASSWORD_MAX} characters.`
    if (next === current) return 'The new password must be different from the current one.'
    if (next !== confirm) return 'The new password and its confirmation do not match.'
    return null
  }

  async function handleSubmit(event) {
    event.preventDefault()
    const problem = validate()
    if (problem) {
      setStatus({ kind: 'error', message: problem })
      return
    }
    setStatus({ kind: 'saving' })
    try {
      await changePassword({ currentPassword: current, newPassword: next })
    } catch (error) {
      // A wrong current password arrives as a 401 with its own message —
      // shown here, and (see api/client.js) it does NOT sign the user out.
      setStatus({ kind: 'error', message: error.detail || 'Could not change the password. Please try again.' })
      return
    }
    // Signed out on success; RequireAuth then sends them to /login, which
    // shows the notice. The old token isn't revoked server-side
    // (stateless JWTs — see the README's Auth section), so this is about
    // making the user prove the new password works, not about
    // invalidating anything.
    logout({
      notice: 'Password changed. Please sign in with your new password.',
      forgetLocation: true,
    })
  }

  const busy = status.kind === 'saving'

  return (
    <section className="panel profile-section">
      <h2 className="panel__title">Change password</h2>
      <form className="form" onSubmit={handleSubmit}>
        <label className="form-field">
          <span>Current password</span>
          <input
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <label className="form-field">
          <span>New password</span>
          <input
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            autoComplete="new-password"
            required
          />
        </label>
        <label className="form-field">
          <span>Confirm new password</span>
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            required
          />
        </label>
        <p className="form-hint">
          {PASSWORD_MIN}–{PASSWORD_MAX} characters. You&apos;ll be signed out after changing it and asked
          to sign in again with the new password.
        </p>
        <div className="form-actions">
          <button type="submit" className="form-submit" disabled={busy}>
            {busy ? 'Changing…' : 'Change password'}
          </button>
          {status.kind === 'error' && (
            <span className="form-message form-message--error" role="alert">
              {status.message}
            </span>
          )}
        </div>
      </form>
    </section>
  )
}

export function ProfilePage() {
  const { user } = useAuth()
  const isBusiness = user.role === 'business'

  return (
    <>
      <h1 className="page-title">Profile &amp; Settings</h1>

      <section className="panel profile-section">
        <h2 className="panel__title">Account</h2>
        <dl className="profile-details">
          <dt>Email</dt>
          <dd>{user.email}</dd>
          <dt>Role</dt>
          <dd>{ROLE_LABEL[user.role] ?? user.role}</dd>
          {isBusiness && (
            <>
              <dt>Business name</dt>
              <dd>{user.business_name}</dd>
              <dt>Owned brands</dt>
              <dd>
                {user.owned_brands.map((brand) => (
                  <span key={brand} className="brand-chip">
                    {brand}
                  </span>
                ))}
              </dd>
            </>
          )}
        </dl>
        {!isBusiness && (
          <p className="panel__note">
            Administrator account — it has no business name or brand ownership of its own, and can
            see every brand.
          </p>
        )}
        {isBusiness && (
          <p className="panel__footnote">
            Email, role and owned brands are set by an administrator and can&apos;t be changed here.
          </p>
        )}
      </section>

      {isBusiness && <BusinessNameForm />}
      <ChangePasswordForm />
    </>
  )
}
