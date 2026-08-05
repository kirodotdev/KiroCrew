/**
 * Same-origin fetch helpers for the Crew Companion gateway proxy.
 *
 * Deliberately low-level: the page tries several candidate proxy paths (see
 * constants.ts) and remembers which one worked, so the path-fallback logic lives
 * with the page state rather than here.
 */

async function readError(r: Response): Promise<string> {
  const body = await r.text().catch(() => '')
  return body || `HTTP ${r.status}`
}

/** GET a JSON document. Throws on a non-2xx response. */
export async function apiGet<T>(path: string): Promise<T> {
  const r = await fetch(path, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(await readError(r))
  return r.json() as Promise<T>
}

/** POST a JSON body, tolerating an empty response. Throws on a non-2xx response. */
export async function apiPost<T = unknown>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  if (!r.ok) throw new Error(await readError(r))
  const text = await r.text()
  return (text ? JSON.parse(text) : {}) as T
}
