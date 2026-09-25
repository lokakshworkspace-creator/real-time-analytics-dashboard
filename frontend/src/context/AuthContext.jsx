import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { getMe, login as apiLogin, setAuthToken, setUnauthorizedHandler } from '../api/client'

const AuthContext = createContext(null)

// The JWT lives in React state ONLY — never localStorage/sessionStorage.
// That's a deliberate trade-off, not an oversight: it means a page
// refresh logs the user out (there's nothing to rehydrate from), but it
// also means the token can never be read by a second tab, survive past
// the browser session, or leak through an XSS payload reading storage —
// a reasonable trade for a portfolio dashboard where "stay logged in
// across refreshes" isn't worth the larger attack surface. See the
// README's auth section for the same note.
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [token, setToken] = useState(null)
  // Why the user is looking at the login screen, and whether to send
  // them back where they were after they sign in. Carried here rather
  // than in router state on purpose: signing out and redirecting to
  // /login are two updates (auth state, and the route guard's redirect)
  // that React Router applies in different priorities, so a message
  // attached to one navigation can be overwritten by the other. Context
  // is set in the same update that clears the session, so it can't lose
  // that race.
  //   notice          shown on the login screen (e.g. "Password changed…")
  //   forgetLocation  true for a deliberate sign-out / password change:
  //                   the next login starts at the landing page instead
  //                   of resuming the page the user was on. False (the
  //                   default) keeps the resume behaviour for an expired
  //                   session and for a typed deep link.
  const [logoutInfo, setLogoutInfo] = useState({ notice: null, forgetLocation: false })

  const logout = useCallback((info) => {
    setUser(null)
    setToken(null)
    setAuthToken(null)
    setLogoutInfo({ notice: info?.notice ?? null, forgetLocation: info?.forgetLocation ?? false })
  }, [])

  // Registered once: api/client.js calls this on any 401 from any
  // endpoint, so an expired/revoked token logs the user out and drops
  // them back to the login screen no matter which component's fetch
  // happened to notice first — and they resume where they were.
  useEffect(() => {
    setUnauthorizedHandler(() => logout({ notice: 'Your session expired. Please sign in again.' }))
  }, [logout])

  const login = useCallback(async (email, password) => {
    const { access_token: accessToken } = await apiLogin(email, password)
    setAuthToken(accessToken)
    // The JWT payload carries role/owned_brands but not business_name
    // (see security.py's create_access_token) — fetching the full
    // profile once here means every consumer of `user` (the brand
    // switcher, the "Viewing: Nike" header) has one complete, real
    // shape to work with instead of two partial ones.
    const profile = await getMe()
    setToken(accessToken)
    setUser(profile)
    // logoutInfo is deliberately NOT reset here: LoginPage is still
    // mounted for a beat after this resolves (its own redirect runs in
    // the same tick), and clearing forgetLocation now made its
    // "already signed in" branch resume the old page instead of the
    // landing page. It only matters while signed out, and every logout()
    // overwrites both fields, so there is nothing stale to clean up.
  }, [])

  // Replaces the stored profile after the user edits it (e.g. renaming
  // their business on the profile page) so every consumer — the navbar's
  // "Viewing: X" badge included — shows the new value without a re-login.
  const updateUser = useCallback((profile) => setUser(profile), [])

  const value = useMemo(
    () => ({
      user,
      token,
      isAuthenticated: user !== null,
      loginNotice: logoutInfo.notice,
      forgetLocation: logoutInfo.forgetLocation,
      login,
      logout,
      updateUser,
    }),
    [user, token, logoutInfo, login, logout, updateUser]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === null) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
