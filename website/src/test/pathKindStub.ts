/**
 * Test stub for the batched path-kind probe (`POST /api/file-kinds`).
 *
 * Every surface that asks whether a transcript path exists goes through
 * `usePathKind`, which sends the paths of one React commit as one batch. Tests
 * that used to answer a per-path `HEAD /api/file-read` answer the batch here
 * instead, from a function of the path.
 */
import { vi } from 'vitest'

export type StubKind = 'file' | 'dir' | null

/** True when this fetch call is the batched kind probe. */
export function isKindsRequest(url: unknown, init?: RequestInit): boolean {
  return String(url) === '/api/file-kinds' && (init?.method ?? 'GET').toUpperCase() === 'POST'
}

/** The paths one kind-probe fetch call asked about. */
export function kindsRequestPaths(init?: RequestInit): string[] {
  try {
    const body = JSON.parse(String(init?.body ?? '{}')) as { paths?: unknown }
    return Array.isArray(body.paths) ? body.paths.map(String) : []
  } catch {
    return []
  }
}

/** The batch reply for `paths`, each classified by `kindOf` (`null` = missing). */
export function kindsResponse(paths: string[], kindOf: (path: string) => StubKind): Response {
  const kinds: Record<string, string> = {}
  for (const p of paths) kinds[p] = kindOf(p) ?? 'missing'
  return new Response(JSON.stringify({ kinds }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * A `fetch` mock that answers the kind probe from `kindOf` and hands every
 * other request to `other` (default: a 404).
 */
export function pathKindsFetch(
  kindOf: (path: string) => StubKind,
  other?: (url: unknown, init?: RequestInit) => Promise<Response>,
) {
  return vi.fn((url: unknown, init?: RequestInit) => {
    if (isKindsRequest(url, init)) return Promise.resolve(kindsResponse(kindsRequestPaths(init), kindOf))
    return other ? other(url, init) : Promise.resolve(new Response('', { status: 404 }))
  })
}

/** Install {@link pathKindsFetch} as the global `fetch`. Returns the mock. */
export function stubPathKinds(
  kindOf: (path: string) => StubKind,
  other?: (url: unknown, init?: RequestInit) => Promise<Response>,
) {
  const fn = pathKindsFetch(kindOf, other)
  globalThis.fetch = fn as unknown as typeof fetch
  return fn
}

/** Every path the mock's kind-probe calls asked about, in call order. */
export function probedPaths(fn: { mock: { calls: unknown[][] } }): string[] {
  return fn.mock.calls
    .filter(([url, init]) => isKindsRequest(url, init as RequestInit | undefined))
    .flatMap(([, init]) => kindsRequestPaths(init as RequestInit | undefined))
}

/** How many kind-probe requests the mock answered. */
export function kindsRequestCount(fn: { mock: { calls: unknown[][] } }): number {
  return fn.mock.calls.filter(([url, init]) => isKindsRequest(url, init as RequestInit | undefined)).length
}
