/**
 * Personal Shopper API client — thin wrapper around fetch with same-origin credentials.
 */

const BASE = '/api/apps/personal-shopper'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { credentials: 'same-origin', ...init })
  if (!res.ok) {
    // Prefer `code` over `error`. The backend's `error` is untranslated English
    // prose meant for logs; `code` is the contract the UI can localize. Falling
    // back to prose would put raw English into a localized surface, which is the
    // whole reason the code exists (RFC 9457 3.1.3).
    let message = `http_${res.status}`
    try {
      const body = await res.json()
      if (body && typeof body.code === 'string') message = body.code
    } catch { /* non-JSON */ }
    throw new Error(message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export function get<T>(path: string): Promise<T> {
  return request<T>(path)
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function put<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function del<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' })
}
