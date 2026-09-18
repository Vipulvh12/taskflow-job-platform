// `import.meta.env` only exists under Vite. Optional-chaining it means this
// module can also be imported directly by a Node test runner, which is how the
// refresh-and-retry logic below gets verified without driving a browser.
const BASE_URL =
  import.meta.env?.VITE_API_BASE_URL ??
  globalThis.process?.env?.VITE_API_BASE_URL ??
  "http://localhost:8000";

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

// Tokens live only in these module-level variables — never localStorage or
// sessionStorage. localStorage is readable by any script on the page, so one
// successful XSS anywhere (a dependency, a stray dangerouslySetInnerHTML)
// exfiltrates every stored token at once. In memory, an attacker's script only
// reaches what is in this tab's heap while it runs.
//
// The cost is real and accepted: a hard refresh clears both tokens and logs the
// user out. The production fix is an httpOnly refresh cookie plus CSRF
// protection, which is V2/V3 work.
let accessToken = null;
let refreshToken = null;
let onAuthChange = null; // AuthContext registers itself here

export function setTokens(tokens) {
  accessToken = tokens?.access_token ?? null;
  refreshToken = tokens?.refresh_token ?? null;
}

export function clearTokens() {
  accessToken = null;
  refreshToken = null;
}

export function getAccessToken() {
  return accessToken;
}

/**
 * Revokes the refresh token server-side, then clears local state.
 *
 * The server call is best-effort: Phase 4's logout deletes the token's jti
 * from Redis, which is what makes it real revocation rather than the client
 * forgetting a string. But if that request fails, we still clear locally —
 * leaving someone "logged in" because the network blipped is the worse
 * outcome, and the refresh token expires on its own regardless.
 */
export async function logout() {
  const token = refreshToken;
  clearTokens();
  if (!token) return;
  try {
    await rawFetch("/auth/logout", {
      method: "POST",
      body: JSON.stringify({ refresh_token: token }),
    });
  } catch {
    /* already cleared locally; nothing useful to surface to the user */
  }
}

export function registerAuthChangeCallback(fn) {
  onAuthChange = fn;
}

async function rawFetch(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;

  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers });
  if (res.status === 204) return null;

  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new ApiError(
      body?.error?.code ?? "unknown_error",
      body?.error?.message ?? "Something went wrong.",
      res.status,
    );
  }
  return body;
}

// Guards against two concurrent 401s each firing their own refresh. The first
// rotates the refresh token; the second would then present a token the server
// has already revoked (Phase 4 rotates on every refresh), logging the user out
// for no reason. Sharing one in-flight promise means both callers wait on the
// same refresh.
let refreshInFlight = null;

async function tryRefresh() {
  if (!refreshToken) return false;
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const body = await rawFetch("/auth/refresh", {
        method: "POST",
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      setTokens(body);
      return true;
    } catch {
      clearTokens();
      onAuthChange?.(null);
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

// Auth endpoints are excluded from retry-on-401: retrying them would either
// loop forever (refresh failing on itself) or re-present the very credential
// that just failed.
const AUTH_PATHS = ["/auth/login", "/auth/register", "/auth/refresh"];

export async function apiFetch(path, options = {}) {
  try {
    return await rawFetch(path, options);
  } catch (err) {
    const isAuthEndpoint = AUTH_PATHS.some((p) => path.startsWith(p));
    if (err instanceof ApiError && err.status === 401 && !isAuthEndpoint) {
      if (await tryRefresh()) return rawFetch(path, options);
    }
    throw err;
  }
}
