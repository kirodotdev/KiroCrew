/**
 * Dev Fleet API client — thin fetch wrapper for the app's two server namespaces.
 *
 * Most routes are served by the SPAWNED backend, reverse-proxied at
 * `/apps/dev-fleet/api/...`. Two are served by the gateway process itself under
 * `/api/apps/dev-fleet/...` — `make-live` and `restart-gateway` — because they
 * touch the live-target pointer (or its cutover latch), which is masked from the
 * sandboxed backend and everything it spawns. `postGateway` targets that
 * namespace; the request body and response shape are unchanged from when the
 * backend served them.
 *
 * Failures are raised as the dashboard's own `ApiError` (from `api/client`),
 * which already carries the status and the raw body: this module only needs the
 * error SHAPE, not that client's request pipeline.
 */
import { ApiError, friendlyErrText } from '../api/client'

const BASE = '/apps/dev-fleet/api'
export const GATEWAY_BASE = '/api/apps/dev-fleet'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function request<T = any>(path: string, opts?: RequestInit, base: string = BASE): Promise<T> {
  const url = base + path
  const res = await fetch(url, { credentials: 'same-origin', ...opts })
  if (!res.ok) {
    // Reuse the dashboard's own error shape rather than a second one: some
    // refusals are states to act on, not failures to report, and the caller
    // needs the STATUS to tell which -- the sync single-flight 409 names the run
    // already in flight, which is what lets the page attach its progress
    // stepper to it. `friendlyErrText` also unwraps the body's `error` field, so
    // a refusal no longer reaches a toast as a raw JSON blob, and it maps the
    // tunnel edge's opaque 429 to something a person can read.
    const text = await res.text().catch(() => '')
    throw new ApiError(res.status, friendlyErrText(res.status, text) || `HTTP ${res.status}`, text)
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

/** POST to a route the GATEWAY process serves (`/api/apps/dev-fleet/...`). */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function postGateway<T = any>(path: string, body: unknown): Promise<T> {
  return request<T>(
    path,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
    GATEWAY_BASE,
  )
}
