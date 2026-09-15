import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import { api, SLASH_COMMANDS_TIMEOUT_MS } from '../api/client'

/* The deadline lives inside `api.slashCommands`, not at its initiator. There is
 * one react-query initiator today (SlashCommandMenu), so this is not yet about
 * losing a dedupe race — it is about the NEXT caller of ['slash-commands']
 * inheriting the bound instead of having to remember it. */

/** A wedged gateway: the response settles ONLY if the signal aborts. Given no
 *  signal it can never settle, which is the pre-fix shape. */
const wedgedFetch = vi.fn((_url: string, init?: RequestInit) =>
  new Promise<Response>((_resolve, reject) => {
    const s = init?.signal
    if (!s) return
    if (s.aborted) return reject(s.reason)
    s.addEventListener('abort', () => reject(s.reason), { once: true })
  }))

const realTimeout = globalThis.setTimeout

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('fetch', wedgedFetch)
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

/** Shrink the deadline without touching the production composition. */
function shrinkDeadline(ms: number, record?: (asked: number) => void) {
  vi.spyOn(globalThis, 'setTimeout').mockImplementation(((fn: () => void, asked?: number) => {
    record?.(asked ?? 0)
    return realTimeout(fn, ms)
  }) as unknown as typeof globalThis.setTimeout)
}

describe('api.slashCommands is bounded at the client, so every call site inherits it', () => {
  it('rejects with TimeoutError when NO signal is passed — the previously-unbounded shape', async () => {
    // Pre-fix this promise never settled at all, so the menu spun forever.
    shrinkDeadline(20)
    await expect(api.slashCommands()).rejects.toSatisfy(
      e => (e as Error)?.name === 'TimeoutError')
    expect(wedgedFetch).toHaveBeenCalledTimes(1)
    expect((wedgedFetch.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal)
  })

  it('relays a caller signal, so react-query unmount/cancel still aborts', async () => {
    // The deadline is long here on purpose: the abort under test is the OUTER
    // one, so a short deadline could win the race and pass for the wrong reason.
    shrinkDeadline(10_000)
    const ac = new AbortController()
    const p = api.slashCommands(ac.signal)
    ac.abort(new DOMException('unmounted', 'AbortError'))
    await expect(p).rejects.toSatisfy(e => (e as Error)?.name === 'AbortError')
  })

  it('asks for SLASH_COMMANDS_TIMEOUT_MS, matching the sibling menu', async () => {
    // Pinned on the value requested, not elapsed wall-clock: a 15s test.
    const asked: number[] = []
    shrinkDeadline(20, ms => asked.push(ms))
    await expect(api.slashCommands()).rejects.toThrow()
    expect(asked).toContain(SLASH_COMMANDS_TIMEOUT_MS)
  })

  it('resolves untouched when the gateway answers in time', async () => {
    // Negative control on the deadline: it must not fail a healthy fetch.
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(
      new Response(JSON.stringify([{ name: '/help', description: 'Show help' }]),
        { status: 200, headers: { 'Content-Type': 'application/json' } }))))
    await expect(api.slashCommands()).resolves
      .toEqual([{ name: '/help', description: 'Show help' }])
  })

  // `slashCommands` is excluded from the generic every-method URL sweep, which
  // passes positional junk that would land on the signal. This replaces it.
  it('builds a well-formed path and sends no body', async () => {
    const f = vi.fn(() => Promise.resolve(
      new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } })))
    vi.stubGlobal('fetch', f)
    await api.slashCommands()
    expect(f).toHaveBeenCalledTimes(1)
    const url = f.mock.calls[0][0] as unknown as string
    expect(typeof url).toBe('string')
    expect(url).toBe('/api/slash-commands')
    for (const junk of ['undefined', '[object Object]', 'NaN', '/null']) {
      expect(url.includes(junk), `leaked ${junk} into ${url}`).toBe(false)
    }
    // A GET carries no body.
    expect((f.mock.calls[0][1] as RequestInit | undefined)?.body).toBeUndefined()
  })
})
