import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { ProtectedRoute } from "./auth/ProtectedRoute";
import { Login } from "./pages/Login";
import { Register } from "./pages/Register";

function JobsPlaceholder() {
  // Phase 11 replaces this with the real job list.
  const { user, logout } = useAuth();
  return (
    <main className="auth-page">
      <div className="card">
        <h1>Signed in</h1>
        <p>
          {user.email}
          {user.is_admin ? " (admin)" : ""}
        </p>
        <p className="muted">The job list arrives in Phase 11.</p>
        <button type="button" onClick={logout}>
          Log out
        </button>
      </div>
    </main>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route
            path="/jobs"
            element={
              <ProtectedRoute>
                <JobsPlaceholder />
              </ProtectedRoute>
            }
          />
          <Route path="*" element={<Navigate to="/jobs" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
