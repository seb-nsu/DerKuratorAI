// The only way the React front end reaches data: the HTTP API with the signed-in user's token. Types of every
// request and answer are generated from the API contract (npm run gen:api), so a contract change breaks the build.
import type { paths } from "./schema";

export type { paths };
const BASE = "/api/v1";
// localStorage (deliberate, see docs/API.md "Sign-in"): a link opened in a new tab stays signed in. The token ends with
// the session timeout; every API call renews it once less than RENEW_BEFORE_S remain, so the timeout acts on
// inactivity. Sign-out revokes the session on the server.
const TOKEN_KEY = "dk.token";
const RENEW_BEFORE_S = 10 * 60;

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export const session = {
  token: () => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
  /** Seconds until the token expires (read from its signed claims; the server checks them again). */
  remaining: (): number | null => {
    const body = session.token()?.split(".")[0];
    if (!body) return null;
    try {
      const claims = JSON.parse(atob(body.replace(/-/g, "+").replace(/_/g, "/")));
      return typeof claims.exp === "number" ? claims.exp - Date.now() / 1000 : null;
    } catch {
      return null;
    }
  },
  /** Ends the session on the server too, then forgets the token. */
  signOut: async () => {
    if (session.token()) await request("POST", "/auth/sign-out").catch(() => undefined);
    session.clear();
  },
};

let renewing: Promise<void> | null = null;

/** A fresh token of the same session when the current one is about to end (one renewal at a time). */
function renewIfDue(path: string): Promise<void> {
  const left = session.remaining();
  if (path.startsWith("/auth/") || left === null || left <= 0 || left > RENEW_BEFORE_S) return Promise.resolve();
  renewing ??= request<{ access_token: string }>("POST", "/auth/refresh")
    .then((answer) => session.set(answer.access_token), () => undefined) // refused: the token runs out as before
    .finally(() => { renewing = null; });
  return renewing;
}

async function request<T>(method: string, path: string, body?: unknown, params?: Record<string, string | undefined>): Promise<T> {
  await renewIfDue(path);
  const query = params ? new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined) as [string, string][]).toString() : "";
  const token = session.token();
  const response = await fetch(`${BASE}${path}${query ? `?${query}` : ""}`, {
    method,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) session.clear(); // expired or revoked: the page frame sends the user to sign-in
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    let detail = data?.detail ?? data?.message; // an admin action answers kind + message
    if (Array.isArray(detail)) detail = detail.slice(0, 3).map((d: { msg?: string }) => d.msg ?? String(d)).join("; ");
    else if (detail && typeof detail === "object") detail = detail.message ?? JSON.stringify(detail); // with its list of problems
    throw new ApiError(response.status, String(detail ?? `Request failed (${response.status}).`));
  }
  return data as T;
}

export const api = {
  get: <T,>(path: string, params?: Record<string, string | undefined>) => request<T>("GET", path, undefined, params),
  post: <T,>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  put: <T,>(path: string, body: unknown = {}) => request<T>("PUT", path, body),
  delete: <T,>(path: string, body?: unknown) => request<T>("DELETE", path, body),
};

type Json<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  responses: { 200: { content: { "application/json": infer R } } };
} ? R : unknown;
export type Answer<P extends keyof paths, M extends keyof paths[P] = "get" & keyof paths[P]> = Json<P, M>;
