import { Navigate } from "react-router-dom";
import { useAuth } from "./AuthContext";

export function ProtectedRoute({ children }) {
  const { user } = useAuth();
  // Nothing is persisted across a reload (tokens are memory-only), so there is
  // no "still checking auth" state to render — `user` is null until a login or
  // register populates it.
  if (!user) return <Navigate to="/login" replace />;
  return children;
}
