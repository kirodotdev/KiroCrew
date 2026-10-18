/**
 * The fixtures the uiPrefs*.test.ts files share: the synced-document keys, a
 * fetch stub, and readers for the stored roster and the last PUT body.
 */
import { vi } from 'vitest'

export const SYNCED_KEYS_KEY = 'mc-ui-prefs-synced'
export const ROSTER_ENTRY = 'mc:ui-prefs:roster'

/** The reconciled roster stored inside the synced document, or null. */
export function storedRoster(): string[] | null {
  const raw = localStorage.getItem(SYNCED_KEYS_KEY)
  if (raw === null) return null
  const entry = (JSON.parse(raw) as Record<string, string>)[ROSTER_ENTRY]
  return typeof entry === 'string' ? (JSON.parse(entry) as string[]) : null
}

export function mockFetch(impl: (url: string, init?: RequestInit) => unknown) {
  const spy = vi.fn((url: string, init?: RequestInit) => Promise.resolve(impl(url, init)))
  vi.stubGlobal('fetch', spy as unknown as typeof fetch)
  return spy
}

export function okJson(body: unknown) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) }
}

/** The last PUT body the spy saw, parsed. */
export function lastPatch(spy: ReturnType<typeof mockFetch>): Record<string, string | null> {
  const puts = spy.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
  const body = (puts.at(-1)?.[1] as RequestInit).body as string
  return JSON.parse(body).prefs
}
