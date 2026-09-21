import { Navigate } from "react-router-dom";
import { useAuth } from "./AuthContext";

/**
 * Hides admin pages from non-admins. This is presentation only — the API's
 * require_admin dependency is the actual check, and returns 403 regardless of
 * what the frontend renders.
 */
export function AdminRoute({ children }) {
  const { user } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  if (!user.is_admin) return <Navigate to="/jobs" replace />;
  return children;
}
