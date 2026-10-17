/**
 * Same-origin fetch helpers for the Crew Companion gateway proxy.
 *
 * Deliberately low-level: the page tries several candidate proxy paths (see
 * constants.ts) and remembers which one worked, so the path-fallback logic lives
 * with the page state rather than here.
 */

/**
 * A failed request, carrying the HTTP status and the backend's machine-readable
 * `code` (see the error helpers in routes.py). The page switches on `code` to
 * tell "the app is disabled" (`app_disabled`, a state with its own UI) apart from
 * a genuine failure — the message alone cannot be trusted for that, since it is
 * localized prose.
 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function toError(r: Response): Promise<ApiError> {
  const text = await r.text().catch(() => '')
  let code = ''
  let message = text || `HTTP ${r.status}`
  if (text) {
    try {
      const parsed = JSON.parse(text) as { error?: unknown; code?: unknown }
      if (typeof parsed.code === 'string') code = parsed.code
      if (typeof parsed.error === 'string') message = parsed.error
    } catch {
      /* not JSON — keep the raw text as the message */
    }
  }
  return new ApiError(r.status, code, message)
}

/** GET a JSON document. Throws an {@link ApiError} on a non-2xx response. */
export async function apiGet<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin' })
  if (!r.ok) throw await toError(r)
  return r.json() as Promise<T>
}

/** POST a JSON body, tolerating an empty response. Throws {@link ApiError} on non-2xx. */
export async function apiPost<T = unknown>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  if (!r.ok) throw await toError(r)
  const text = await r.text()
  return (text ? JSON.parse(text) : {}) as T
}
