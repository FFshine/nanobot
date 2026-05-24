import type { BootPayload, User } from "./types";

const STORAGE_KEY_TOKEN = "nanobot-webui.jwt";
const STORAGE_KEY_USER = "nanobot-webui.user";

let _token: string | null = sessionStorage.getItem(STORAGE_KEY_TOKEN);
let _user: User | null = null;

try {
  const raw = sessionStorage.getItem(STORAGE_KEY_USER);
  if (raw) _user = JSON.parse(raw);
} catch {
  // ignore
}

export interface AuthState {
  token: string | null;
  user: User | null;
  isAuthenticated: boolean;
}

export function getAuthState(): AuthState {
  return { token: _token, user: _user, isAuthenticated: _token !== null && _user !== null };
}

export function getToken(): string | null {
  return _token;
}

export function getUser(): User | null {
  return _user;
}

export function getAuthHeaders(): Record<string, string> {
  if (_token) {
    return { Authorization: `Bearer ${_token}` };
  }
  return {};
}

export function setAuth(token: string, user: User): void {
  _token = token;
  _user = user;
  sessionStorage.setItem(STORAGE_KEY_TOKEN, token);
  sessionStorage.setItem(STORAGE_KEY_USER, JSON.stringify(user));
}

export function clearAuth(): void {
  _token = null;
  _user = null;
  sessionStorage.removeItem(STORAGE_KEY_TOKEN);
  sessionStorage.removeItem(STORAGE_KEY_USER);
}

export function isAdmin(): boolean {
  return _user?.role === "admin";
}

export async function fetchBootstrap(
  username: string,
  password: string,
): Promise<BootPayload> {
  const resp = await fetch("/webui/bootstrap", {
    method: "GET",
    headers: {
      Authorization: `Basic ${btoa(`${username}:${password}`)}`,
    },
  });
  if (!resp.ok) {
    if (resp.status === 401) {
      throw new Error("Invalid credentials");
    }
    throw new Error(`Bootstrap failed: ${resp.status}`);
  }
  return resp.json();
}

export async function checkBootstrapWithoutAuth(): Promise<{ has_users: boolean }> {
  const resp = await fetch("/webui/bootstrap");
  if (!resp.ok) return { has_users: true };
  return resp.json();
}

export async function setupAdmin(username: string, password: string): Promise<User> {
  const params = new URLSearchParams();
  params.set("username", username);
  params.set("password", password);
  const resp = await fetch(`/api/auth/setup?${params.toString()}`);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail || `Setup failed: ${resp.status}`);
  }
  const body = await resp.json() as { ok: boolean; user: User };
  return body.user;
}
