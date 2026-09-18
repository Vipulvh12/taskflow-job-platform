import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { ProtectedRoute } from "./auth/ProtectedRoute";
import { JobDetail } from "./pages/JobDetail";
import { JobList } from "./pages/JobList";
import { Login } from "./pages/Login";
import { Register } from "./pages/Register";
import { SubmitJob } from "./pages/SubmitJob";

function Shell({ children }) {
  const { user, logout } = useAuth();
  return (
    <div className="shell">
      <nav className="topbar">
        <Link to="/jobs" className="brand">
          TaskFlow
        </Link>
        <span className="spacer" />
        <span className="muted">
          {user.email}
          {user.is_admin ? " (admin)" : ""}
        </span>
        <button type="button" className="secondary" onClick={logout}>
          Log out
        </button>
      </nav>
      <main className="content">{children}</main>
    </div>
  );
}

function Protected({ children }) {
  return (
    <ProtectedRoute>
      <Shell>{children}</Shell>
    </ProtectedRoute>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          {/* /jobs/new is a static segment, so react-router ranks it above
              /jobs/:id regardless of declaration order — listed first anyway
              so the intent is obvious to a reader. */}
          <Route
            path="/jobs/new"
            element={
              <Protected>
                <SubmitJob />
              </Protected>
            }
          />
          <Route
            path="/jobs/:id"
            element={
              <Protected>
                <JobDetail />
              </Protected>
            }
          />
          <Route
            path="/jobs"
            element={
              <Protected>
                <JobList />
              </Protected>
            }
          />
          <Route path="*" element={<Navigate to="/jobs" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
