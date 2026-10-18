/**
 * The fixtures the ApiClient.coverage*.test.tsx files share: the hand-rolled
 * Response stub, the `fetch` mock every file stubs in, and `call()`, which reads
 * one request back off it.
 */
import { vi } from 'vitest'

type Init = RequestInit & { headers?: Record<string, string> }

export function res(
  status: number,
  body: unknown,
  opts: { headers?: Record<string, string>; text?: string; url?: string } = {},
): Response {
  const text = opts.text ?? (typeof body === 'string' ? body : JSON.stringify(body))
  return {
    ok: status >= 200 && status < 300,
    status,
    url: opts.url ?? 'http://localhost:6776/api/probe',
    headers: { get: (k: string) => opts.headers?.[k] ?? opts.headers?.[k.toLowerCase()] ?? null },
    json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
    text: async () => text,
    blob: async () => new Blob([text]),
    clone: () => res(status, body, opts),
  } as unknown as Response
}

export const okJson = (body: unknown = { ok: true }) => res(200, body)

export const fetchMock = vi.fn()

/** The nth fetch call, destructured the way every coverage assertion wants it. */
export function call(n = 0) {
  const [url, init] = fetchMock.mock.calls[n] as [string, Init | undefined]
  return {
    url,
    init,
    method: init?.method,
    headers: (init?.headers ?? {}) as Record<string, string>,
    // Only JSON bodies are decoded; a multipart upload stays a FormData.
    body: typeof init?.body === 'string'
      ? (JSON.parse(init.body) as Record<string, unknown>)
      : undefined,
  }
}
