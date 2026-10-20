// Thin client for the elastic-evals API. Auth: an API key ("key_id.secret") kept in this
// browser, or, against a dev server (EE_AUTH=off), just a tenant name.
export type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any

function settings() {
  try {
    return { key: localStorage.getItem("ee.key") || "", tenant: localStorage.getItem("ee.tenant") || "" };
  } catch {
    return { key: "", tenant: "" };
  }
}

export function saveSettings(key: string, tenant: string) {
  try {
    localStorage.setItem("ee.key", key);
    localStorage.setItem("ee.tenant", tenant);
  } catch {
    /* storage unavailable: settings last for this page only */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = Json>(path: string, init: RequestInit & { query?: Record<string, unknown> } = {}): Promise<T> {
  const { key, tenant } = settings();
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init.headers as Record<string, string>) };
  if (key) headers.Authorization = `Bearer ${key}`;
  if (tenant) headers["X-EE-Tenant"] = tenant;
  let url = path;
  if (init.query) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(init.query)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
    const s = q.toString();
    if (s) url += `?${s}`;
  }
  const res = await fetch(url, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export const post = <T = Json>(path: string, body?: unknown, query?: Record<string, unknown>) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body), query });
