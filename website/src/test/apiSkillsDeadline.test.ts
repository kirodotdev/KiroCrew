import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import { api, SKILLS_TIMEOUT_MS } from '../api/client'

/* The deadline lives inside `api.skills`, not at each initiator. That is the
 * whole point: react-query dedupes on the query key, so binding per-caller
 * leaves the promise unbounded whenever an unbounded initiator of the same key
 * wins the race. Two keys are shared in the tree — ['skills'] across
 * HookSkillsSelect / SkillsTab / the command palette, and
 * ['skills', slot, project, agent] across ChatInput's prefetch and
 * SkillPickerMenu — so whichever initiator wins, the fetch is the same one. */

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

describe('api.skills is bounded at the client, so every call site inherits it', () => {
  it('rejects with TimeoutError when NO signal is passed — the previously-unbounded shape', async () => {
    // Three settings-side callers invoke `api.skills()` with no arguments; before
    // the wrap moved here they were unbounded. This is the proof they are not.
    shrinkDeadline(20)
    await expect(api.skills()).rejects.toSatisfy(
      e => (e as Error)?.name === 'TimeoutError')
    expect(wedgedFetch).toHaveBeenCalledTimes(1)
    expect((wedgedFetch.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal)
  })

  it('rejects with TimeoutError for a slot-scoped call too', async () => {
    shrinkDeadline(20)
    await expect(api.skills('dashboard:chat-1', 'agent-x')).rejects.toSatisfy(
      e => (e as Error)?.name === 'TimeoutError')
  })

  it('relays a caller signal, so react-query unmount/cancel still aborts', async () => {
    shrinkDeadline(10_000)
    const ac = new AbortController()
    const p = api.skills(undefined, undefined, ac.signal)
    ac.abort(new DOMException('unmounted', 'AbortError'))
    await expect(p).rejects.toSatisfy(e => (e as Error)?.name === 'AbortError')
  })

  it('asks for SKILLS_TIMEOUT_MS, inside the measured window', async () => {
    // Pinned on the value requested, not elapsed wall-clock: a 15s test.
    const asked: number[] = []
    shrinkDeadline(20, ms => asked.push(ms))
    await expect(api.skills()).rejects.toThrow()
    expect(asked).toContain(SKILLS_TIMEOUT_MS)
    expect(SKILLS_TIMEOUT_MS).toBeGreaterThan(9_760)   // the slow-but-completed observation
    expect(SKILLS_TIMEOUT_MS).toBeLessThan(41_410)     // the first pathological one
  })

  it('resolves untouched when the gateway answers in time', async () => {
    // Negative control on the deadline: it must not fail a healthy fetch.
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(
      new Response(JSON.stringify([{ key: 'grill', name: 'grill' }]),
        { status: 200, headers: { 'Content-Type': 'application/json' } }))))
    await expect(api.skills()).resolves.toEqual([{ key: 'grill', name: 'grill' }])
  })

  // `skills` is excluded from the generic every-method URL sweep, which passes
  // positional junk. These re-assert exactly what that sweep would have.
  describe('URL construction (replacing the generic sweep\'s coverage)', () => {
    const ok = () => vi.fn(() => Promise.resolve(
      new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } })))

    it.each([
      ['no arguments', [] as const, '/api/skills'],
      ['a slot only', ['dashboard:chat-1'] as const, '/api/skills'],
      ['an agent', [undefined, 'custom template'] as const, '/api/skills?agent=custom%20template'],
    ])('builds a well-formed path with %s', async (_label, args, expected) => {
      const f = ok()
      vi.stubGlobal('fetch', f)
      await api.skills(...(args as Parameters<typeof api.skills>))
      expect(f).toHaveBeenCalledTimes(1)
      const url = f.mock.calls[0][0] as unknown as string
      expect(typeof url).toBe('string')
      expect(url).toBe(expected)
      expect(url.startsWith('/api/')).toBe(true)
      for (const junk of ['undefined', '[object Object]', 'NaN', '/null']) {
        expect(url.includes(junk), `leaked ${junk} into ${url}`).toBe(false)
      }
      // A GET carries no body.
      expect((f.mock.calls[0][1] as RequestInit | undefined)?.body).toBeUndefined()
    })
  })
})
