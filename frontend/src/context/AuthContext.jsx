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

  const logout = useCallback(() => {
    setUser(null)
    setToken(null)
    setAuthToken(null)
  }, [])

  // Registered once: api/client.js calls this on any 401 from any
  // endpoint, so an expired/revoked token logs the user out and drops
  // them back to the login screen no matter which component's fetch
  // happened to notice first.
  useEffect(() => {
    setUnauthorizedHandler(logout)
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
  }, [])

  const value = useMemo(
    () => ({ user, token, isAuthenticated: user !== null, login, logout }),
    [user, token, login, logout]
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
