import './App.css'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AppLayout } from './components/AppLayout'
import { LoginPage } from './components/LoginPage'
import { RequireAdmin } from './components/RequireAdmin'
import { RequireAuth } from './components/RequireAuth'
import { AuthProvider } from './context/AuthContext'
import { AnomaliesPage } from './pages/AnomaliesPage'
import { BenchmarkPage } from './pages/BenchmarkPage'
import { OverviewPage } from './pages/OverviewPage'
import { ProductsPage } from './pages/ProductsPage'
import { ProfilePage } from './pages/ProfilePage'

// Routes, outermost guard first:
//   /login                       public
//   RequireAuth                  everything below: signed out -> /login
//     AppLayout                  persistent navbar + the page
//       /overview /products /anomalies /profile
//       RequireAdmin
//         /benchmark             admin only, guarded here as well as hidden in the navbar
// "/" and any unknown path fall through to /overview (which itself
// bounces to /login when signed out).
//
// Each page's components fetch and poll only their own data (see
// AppLayout) — this file wires routes, not data.
function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<RequireAuth />}>
            <Route element={<AppLayout />}>
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="/products" element={<ProductsPage />} />
              <Route path="/anomalies" element={<AnomaliesPage />} />
              <Route element={<RequireAdmin />}>
                <Route path="/benchmark" element={<BenchmarkPage />} />
              </Route>
              <Route path="/profile" element={<ProfilePage />} />
            </Route>
          </Route>
          <Route path="*" element={<Navigate to="/overview" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}

export default App
