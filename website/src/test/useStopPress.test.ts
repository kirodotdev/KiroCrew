/**
 * useStopPress over the embed's transport (sendEmbedStop) -- the failure,
 * slot-switch, compaction-decline and turn-handoff branches the ChatEmbed
 * render tests do not reach. Each case is a way a Stop press could silently lie
 * to the user: a lost request that cleared the pending state (so the retry read
 * as a fresh soft press and the backend hard-killed the queue without
 * warning), a refusal that kept the optimistic `soft_pending`, or a reply for
 * the previous slot or turn landing on the new one. ChatPane's own Stop tests
 * cover the same hook without the turn and decline extensions.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useStopPress, type StopPhase } from '../chat-core/stop/useStopPress'
import { sendEmbedStop } from '../app-sdk/embedStopTransport'
import { FORCE_KILL_ARMING_MS } from '../utils/stopDebounce'
import { STOP_DECLINED_HINT_MS } from '../hooks/useStopDeclinedHint'

type Props = { slotKey: string; running: boolean; serverStopState?: StopPhase; serverStopDeclined?: boolean; serverTurn?: string }
type Api = { post: <T>(p: string, b: unknown) => Promise<T> }

// The embed's wiring (ChatEmbed): turn-scoped, decline-aware. onSettled fires
// a macrotask after the reply, once the hook has acted on it.
function useEmbedPress(api: Api, onSettled: () => void, p: Props) {
  return useStopPress({
    slotKey: p.slotKey, busy: p.running, serverStopState: p.serverStopState,
    serverStopDeclined: p.serverStopDeclined === true, turnScoped: true, turnKey: p.serverTurn,
    sendStop: (force) => sendEmbedStop(api, p.slotKey, force).finally(() => { setTimeout(onSettled, 0) }),
  })
}

function setup(post: (path: string, body: unknown) => Promise<unknown>, initial: Props) {
  const onSettled = vi.fn()
  const api = { post: vi.fn(post) } as unknown as Api
  const hook = renderHook((p: Props) => useEmbedPress(api, onSettled, p), { initialProps: initial })
  return { hook, api: api as unknown as { post: ReturnType<typeof vi.fn> }, onSettled }
}

describe('useStopPress (embed)', () => {
  it('keeps the pending state on a request that never got an answer', async () => {
    const { hook, onSettled } = setup(() => Promise.reject(new TypeError('Failed to fetch')), {
      slotKey: 's1', running: true,
    })
    act(() => hook.result.current.onStop())
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
    // It may have armed the cancel, so the retry must be a deliberate force.
    expect(hook.result.current.stopState).toBe('soft_pending')
    expect(hook.result.current.stopError).not.toBe('')
  })

  it('clears the pending state on an HTTP error so the retry is a fresh soft press', async () => {
    const err = Object.assign(new Error('boom'), { status: 500 })
    const { hook, onSettled } = setup(() => Promise.reject(err), { slotKey: 's1', running: true })
    act(() => hook.result.current.onStop())
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
    expect(hook.result.current.stopState).toBe('idle')
    // A 5xx is said in plain words; the backend's text is only the detail line.
    expect(hook.result.current.stopError).toBe("The server hit a problem and couldn't stop this turn. Press Stop again.")
    expect(hook.result.current.stopErrorDetail).toContain('boom')
  })

  // #20398: a known refusal is said in plain words, never as the backend's text.
  it('says a missing chat in plain words and drops the raw API text', async () => {
    const { ApiError } = await import('../api/apiError')
    const body = JSON.stringify({ error: 'not found', code: 'slot_not_found' })
    const err = new ApiError(404, `API 404: ${body}`, body)
    const { hook, onSettled } = setup(() => Promise.reject(err), { slotKey: 's1', running: true })
    act(() => hook.result.current.onStop())
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
    expect(hook.result.current.stopError).toBe("This chat wasn't found, so nothing was stopped. Reload the page.")
    expect(hook.result.current.stopErrorDetail).toBe('')
    expect(hook.result.current.stopError).not.toContain('API 404')
  })

  it('says a signed-out stop in plain words', async () => {
    const { ApiError } = await import('../api/apiError')
    const err = new ApiError(403, 'auth required', '', true)
    const { hook, onSettled } = setup(() => Promise.reject(err), { slotKey: 's1', running: true })
    act(() => hook.result.current.onStop())
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
    expect(hook.result.current.stopError).toBe("You're signed out, so nothing was stopped. Sign in again, then press Stop.")
    expect(hook.result.current.stopErrorDetail).toBe('')
  })

  it('shows a proxy sign-in challenge as its own remedy, not as signed out', async () => {
    const { ApiError } = await import('../api/apiError')
    const err = new ApiError(403, 'Your access proxy needs you to sign in. Reload the page.', '', true, true)
    const { hook, onSettled } = setup(() => Promise.reject(err), { slotKey: 's1', running: true })
    act(() => hook.result.current.onStop())
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
    expect(hook.result.current.stopError).toBe('Your access proxy needs you to sign in. Reload the page.')
    expect(hook.result.current.stopError).not.toMatch(/signed out/)
  })

  it('keeps an unknown refusal as a detail line under a plain message', async () => {
    const { hook, onSettled } = setup(() => Promise.resolve({ ok: false, error: 'crew went away', code: 'crew_gone' }), {
      slotKey: 's1', running: true,
    })
    act(() => hook.result.current.onStop())
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
    expect(hook.result.current.stopError).toBe("Couldn't stop this turn. Press Stop again.")
    expect(hook.result.current.stopErrorDetail).toBe('crew went away')
  })

  it('drops a reply that arrives after the embed moved to another slot', async () => {
    let reject!: (e: unknown) => void
    const { hook, onSettled } = setup(
      () => new Promise((_, r) => { reject = r }),
      { slotKey: 's1', running: true },
    )
    act(() => hook.result.current.onStop())
    hook.rerender({ slotKey: 's2', running: true })
    await act(async () => { reject(Object.assign(new Error('late'), { status: 500 })) })
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
    expect(hook.result.current.stopError).toBe('')
    expect(hook.result.current.stopState).toBe('idle')
  })

  it('drops a refusal that arrives after the embed moved to another slot', async () => {
    let resolve!: (v: unknown) => void
    const { hook, onSettled } = setup(
      () => new Promise((r) => { resolve = r }),
      { slotKey: 's1', running: true },
    )
    act(() => hook.result.current.onStop())
    hook.rerender({ slotKey: 's2', running: true })
    await act(async () => { resolve({ ok: false, error: 'late refusal' }) })
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
    expect(hook.result.current.stopError).toBe('')
  })

  it('retires the optimistic phase when the turn ends or the server reports its own', async () => {
    const { hook } = setup(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true })
    act(() => hook.result.current.onStop())
    expect(hook.result.current.stopState).toBe('soft_pending')
    hook.rerender({ slotKey: 's1', running: true, serverStopState: 'killing' })
    expect(hook.result.current.stopState).toBe('killing')
    hook.rerender({ slotKey: 's1', running: false })
    expect(hook.result.current.stopState).toBe('idle')
  })

  it('encodes the slot key in the stop path', async () => {
    const { hook, api, onSettled } = setup(() => Promise.resolve({ ok: true }), { slotKey: 'a/b c', running: true })
    act(() => hook.result.current.onStop())
    expect(api.post).toHaveBeenCalledWith('/api/chat/slots/a%2Fb%20c/stop', {})
    // The reply settles the decline flag; let it land inside act.
    await waitFor(() => expect(onSettled).toHaveBeenCalled())
  })
})

// A press during the session's automatic compaction is DECLINED by the backend
// (`{ok: true, compacting: true}`, stop_state left idle) and arms its hatch: the
// next press, force or not, restarts the session. Fake timers drive both the
// double-tap window and the decline window.
describe('useStopPress (embed) after a compaction decline', () => {
  const DECLINED = { ok: true, info: 'compacting', compacting: true }
  const paths = (api: { post: ReturnType<typeof vi.fn> }) => api.post.mock.calls.map(([p]) => p)
  const flush = () => act(async () => {})

  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z')) })
  afterEach(() => { vi.useRealTimers() })

  async function declined(post?: (path: string) => Promise<unknown>) {
    let n = 0
    const ctx = setup(
      (path) => (n++ === 0 ? Promise.resolve(DECLINED) : (post ? post(path) : Promise.resolve({ ok: true }))),
      { slotKey: 's1', running: true },
    )
    act(() => ctx.hook.result.current.onStop())
    await flush()
    return ctx
  }

  it('drops the optimistic Stopping and arms the reset warning', async () => {
    const { hook } = await declined()
    expect(hook.result.current.stopState).toBe('idle')
    expect(hook.result.current.stopDeclined).toBe(true)
  })

  it('sends the next press as a plain stop and spends the decline when it lands', async () => {
    const { hook, api } = await declined()
    act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 1) })
    act(() => hook.result.current.onStop())
    // The backend's marker escalates it; while compacting it is the reset,
    // once the compaction ended the same press is an ordinary soft stop.
    expect(paths(api)).toEqual(['/api/chat/slots/s1/stop', '/api/chat/slots/s1/stop'])
    expect(hook.result.current.stopState).toBe('soft_pending')
    await flush()
    expect(hook.result.current.stopDeclined).toBe(false)
  })

  it('ignores a double-tap on the declined press inside the arming window', async () => {
    const { hook, api } = await declined()
    act(() => hook.result.current.onStop())
    expect(paths(api)).toHaveLength(1)
    expect(hook.result.current.stopDeclined).toBe(true)
  })

  it('retires the warning with the backend window, so the next press is a fresh soft press', async () => {
    const { hook } = await declined()
    act(() => { vi.advanceTimersByTime(STOP_DECLINED_HINT_MS) })
    expect(hook.result.current.stopDeclined).toBe(false)
  })

  it('retires the warning when the turn ends or the embed changes slot', async () => {
    const a = await declined()
    a.hook.rerender({ slotKey: 's1', running: false })
    expect(a.hook.result.current.stopDeclined).toBe(false)
    const b = await declined()
    b.hook.rerender({ slotKey: 's2', running: true })
    expect(b.hook.result.current.stopDeclined).toBe(false)
  })

  it('lets a stop the server reports supersede the decline', async () => {
    const { hook } = await declined()
    // A stop from another surface: the poll shows a phase of its own, and the
    // backend's escalation consumed the marker, so idle again is not armed.
    hook.rerender({ slotKey: 's1', running: true, serverStopState: 'soft_pending' })
    hook.rerender({ slotKey: 's1', running: true, serverStopState: 'idle' })
    expect(hook.result.current.stopDeclined).toBe(false)
  })

  it('keeps the warning when the reset press fails outright, since the hatch is still armed', async () => {
    const { hook } = await declined(() => Promise.reject(Object.assign(new Error('boom'), { status: 500 })))
    act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 1) })
    act(() => hook.result.current.onStop())
    await flush()
    expect(hook.result.current.stopErrorDetail).toContain('boom')
    expect(hook.result.current.stopDeclined).toBe(true)
  })

  it('keeps the reset warning while the reset press has no answer, so the retry is still announced', async () => {
    const { hook, api } = await declined(() => Promise.reject(new TypeError('Failed to fetch')))
    act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 1) })
    act(() => hook.result.current.onStop())
    await flush()
    // The press may or may not have reached the backend; either way the next
    // press can be the reset, and the host must still say so.
    expect(hook.result.current.stopError).not.toBe('')
    expect(hook.result.current.stopDeclined).toBe(true)
    act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 1) })
    act(() => hook.result.current.onStop())
    // Still a plain stop: the backend's marker decides whether it is the reset.
    expect(paths(api)).toEqual(['/api/chat/slots/s1/stop', '/api/chat/slots/s1/stop', '/api/chat/slots/s1/stop'])
    await flush()
  })

  // The slot-detail read reports the backend's own marker, so an embed that
  // opens (or returns) mid-window warns before its first press too.
  it('honours a decline the server reports when the embed opens', async () => {
    const { hook, api } = setup(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true, serverStopDeclined: true })
    expect(hook.result.current.stopDeclined).toBe(true)
    act(() => hook.result.current.onStop())
    expect(paths(api)).toEqual(['/api/chat/slots/s1/stop'])
    await flush()
  })

  it('stops warning once the server reports the marker expired or cleared', async () => {
    const { hook } = setup(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true, serverStopDeclined: true })
    hook.rerender({ slotKey: 's1', running: true, serverStopDeclined: false })
    expect(hook.result.current.stopDeclined).toBe(false)
  })

  it('does not warn on an armed server marker once a stop is under way', async () => {
    const { hook } = setup(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true, serverStopDeclined: true, serverStopState: 'killing' })
    expect(hook.result.current.stopDeclined).toBe(false)
  })

  it('does not let a late decline re-arm the warning over the press that followed it', async () => {
    let answerFirst!: (v: unknown) => void
    let n = 0
    const { hook } = setup(
      () => (n++ === 0 ? new Promise((r) => { answerFirst = r }) : Promise.resolve({ ok: true })),
      { slotKey: 's1', running: true },
    )
    act(() => hook.result.current.onStop())
    act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 1) })
    act(() => hook.result.current.onStop())
    await flush()
    await act(async () => { answerFirst(DECLINED) })
    expect(hook.result.current.stopDeclined).toBe(false)
  })

  // A decline answered after its turn ended must not warn on the next turn: the
  // compaction that declined it is over, and the backend dropped its marker.
  it('does not arm a decline that is answered after its turn ended', async () => {
    let answer!: (v: unknown) => void
    const { hook } = setup(() => new Promise((r) => { answer = r }), { slotKey: 's1', running: true })
    act(() => hook.result.current.onStop())
    hook.rerender({ slotKey: 's1', running: false })
    hook.rerender({ slotKey: 's1', running: true })
    await act(async () => { answer(DECLINED) })
    expect(hook.result.current.stopDeclined).toBe(false)
  })

  it('does not arm a decline that is answered after the embed left the slot and came back', async () => {
    let answer!: (v: unknown) => void
    const { hook } = setup(() => new Promise((r) => { answer = r }), { slotKey: 's1', running: true })
    act(() => hook.result.current.onStop())
    hook.rerender({ slotKey: 's2', running: true })
    hook.rerender({ slotKey: 's1', running: true })
    await act(async () => { answer(DECLINED) })
    expect(hook.result.current.stopDeclined).toBe(false)
  })
})

// A soft stop with a message queued: the drain hands the floor straight to the
// next turn, and a 1 s poll can read the successor without ever seeing
// `running: false` or a stop phase. Only the turn identity changes, and the
// new turn must start with no press of its own.
describe('useStopPress (embed) across a queued-turn handoff', () => {
  type TurnProps = Props
  const paths = (api: { post: ReturnType<typeof vi.fn> }) => api.post.mock.calls.map(([p]) => p)
  const flush = () => act(async () => {})

  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z')) })
  afterEach(() => { vi.useRealTimers() })

  function setupTurns(post: (path: string) => Promise<unknown>, initial: TurnProps) {
    const { hook, api } = setup(post, initial)
    return { hook, api }
  }

  it('clears the old turn stop so the new turn first press is a soft stop', async () => {
    const { hook, api } = setupTurns(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true, serverTurn: 'g:4' })
    act(() => hook.result.current.onStop())
    await flush()
    expect(hook.result.current.stopState).toBe('soft_pending')
    // The poll's next read is already the queued turn: still running, idle.
    hook.rerender({ slotKey: 's1', running: true, serverTurn: 'g:5' })
    expect(hook.result.current.stopState).toBe('idle')
    act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 1) })
    act(() => hook.result.current.onStop())
    expect(paths(api)).toEqual(['/api/chat/slots/s1/stop', '/api/chat/slots/s1/stop'])
    expect(hook.result.current.stopState).toBe('soft_pending')
    await flush()
  })

  it('ignores a double-tap whose second tap lands after the read that names the next turn', async () => {
    const { hook, api } = setupTurns(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true, serverTurn: 'g:4' })
    act(() => hook.result.current.onStop())
    await flush()
    // The press's catch-up read already shows the queued successor, mid-stop.
    hook.rerender({ slotKey: 's1', running: true, serverTurn: 'g:5', serverStopState: 'soft_pending' })
    act(() => { vi.advanceTimersByTime(200) })
    act(() => hook.result.current.onStop())
    expect(api.post).toHaveBeenCalledTimes(1)
    await flush()
  })

  it('clears a lost-request pending state and its error with the turn', async () => {
    const { hook } = setupTurns(() => Promise.reject(new TypeError('Failed to fetch')), { slotKey: 's1', running: true, serverTurn: 'g:4' })
    act(() => hook.result.current.onStop())
    await flush()
    expect(hook.result.current.stopState).toBe('soft_pending')
    expect(hook.result.current.stopError).not.toBe('')
    hook.rerender({ slotKey: 's1', running: true, serverTurn: 'g:5' })
    expect(hook.result.current.stopState).toBe('idle')
    expect(hook.result.current.stopError).toBe('')
  })

  it('does not arm a decline answered after the turn changed', async () => {
    let answer!: (v: unknown) => void
    const { hook } = setupTurns(() => new Promise((r) => { answer = r }), { slotKey: 's1', running: true, serverTurn: 'g:4' })
    act(() => hook.result.current.onStop())
    hook.rerender({ slotKey: 's1', running: true, serverTurn: 'g:5' })
    await act(async () => { answer({ ok: true, compacting: true }) })
    expect(hook.result.current.stopDeclined).toBe(false)
    expect(hook.result.current.stopState).toBe('idle')
  })

  it('keeps a press made before the turn was numbered', async () => {
    const { hook } = setupTurns(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true })
    act(() => hook.result.current.onStop())
    await flush()
    // Admission-only running has no identity; the first one names the turn the
    // press was aimed at, not a successor.
    hook.rerender({ slotKey: 's1', running: true, serverTurn: 'g:5' })
    expect(hook.result.current.stopState).toBe('soft_pending')
  })

  it('keeps the state through a read with no identity between two of the same turn', async () => {
    const { hook } = setupTurns(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true, serverTurn: 'g:4' })
    act(() => hook.result.current.onStop())
    await flush()
    hook.rerender({ slotKey: 's1', running: true })
    hook.rerender({ slotKey: 's1', running: true, serverTurn: 'g:4' })
    expect(hook.result.current.stopState).toBe('soft_pending')
  })

  // A late failure for the previous turn's press must not clear the new turn's
  // arming time: a double-tap inside the window would then read as a second
  // press and force-stop the new turn, dropping its queue.
  it.each([
    ['an HTTP error', (settle: { reject: (e: unknown) => void; resolve: (v: unknown) => void }) =>
      settle.reject(Object.assign(new Error('boom'), { status: 500 }))],
    ['a 2xx refusal', (settle: { reject: (e: unknown) => void; resolve: (v: unknown) => void }) =>
      settle.resolve({ ok: false, error: 'peer unreachable' })],
    ['a lost request', (settle: { reject: (e: unknown) => void; resolve: (v: unknown) => void }) =>
      settle.reject(new TypeError('Failed to fetch'))],
  ])('ignores %s for the previous turn press after the new turn was pressed', async (_name, fail) => {
    const pending: { reject: (e: unknown) => void; resolve: (v: unknown) => void }[] = []
    const { hook, api } = setupTurns(
      () => new Promise((resolve, reject) => { pending.push({ resolve, reject }) }),
      { slotKey: 's1', running: true, serverTurn: 'g:4' },
    )
    act(() => hook.result.current.onStop())
    hook.rerender({ slotKey: 's1', running: true, serverTurn: 'g:5' })
    act(() => { vi.advanceTimersByTime(FORCE_KILL_ARMING_MS + 1) })
    act(() => hook.result.current.onStop())
    expect(hook.result.current.stopState).toBe('soft_pending')
    await act(async () => { fail(pending[0]) })
    expect(hook.result.current.stopState).toBe('soft_pending')
    expect(hook.result.current.stopError).toBe('')
    // A double-tap inside the arming window is still ignored, not forced.
    act(() => hook.result.current.onStop())
    expect(paths(api)).toEqual(['/api/chat/slots/s1/stop', '/api/chat/slots/s1/stop'])
  })

  it('treats a gateway restart with the same turn number as a new turn', async () => {
    const { hook } = setupTurns(() => Promise.resolve({ ok: true }), { slotKey: 's1', running: true, serverTurn: 'g:4' })
    act(() => hook.result.current.onStop())
    await flush()
    hook.rerender({ slotKey: 's1', running: true, serverTurn: 'h:4' })
    expect(hook.result.current.stopState).toBe('idle')
  })
})
