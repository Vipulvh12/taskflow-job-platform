import { createContext, useContext, useEffect, useState } from "react";
import {
  apiFetch,
  logout as apiLogout,
  registerAuthChangeCallback,
  setTokens,
} from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // If a silent refresh fails in the background (client.js), this is how
    // every component watching `user` finds out immediately.
    registerAuthChangeCallback(setUser);
  }, []);

  async function login(email, password) {
    setLoading(true);
    try {
      setTokens(
        await apiFetch("/auth/login", {
          method: "POST",
          body: JSON.stringify({ email, password }),
        }),
      );
      setUser(await apiFetch("/auth/me"));
    } finally {
      setLoading(false);
    }
  }

  async function register(email, password) {
    setLoading(true);
    try {
      setTokens(
        await apiFetch("/auth/register", {
          method: "POST",
          body: JSON.stringify({ email, password }),
        }),
      );
      setUser(await apiFetch("/auth/me"));
    } finally {
      setLoading(false);
    }
  }

  async function logout() {
    // Clears local state first, then revokes server-side (best effort).
    setUser(null);
    await apiLogout();
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
