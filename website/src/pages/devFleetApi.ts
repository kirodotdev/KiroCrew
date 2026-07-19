/**
 * Dev Fleet API client — thin fetch wrapper for /apps/dev-fleet/api routes.
 */
const BASE = '/apps/dev-fleet/api'

export interface ApiError {
  error: string
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function request<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const url = BASE + path
  const res = await fetch(url, { credentials: 'same-origin', ...opts })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    const err = new Error(body || `HTTP ${res.status}`) as Error & { status?: number }
    err.status = res.status
    throw err
  }
  return res.json()
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function get<T = any>(path: string): Promise<T> {
  return request<T>(path)
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function post<T = any>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}
