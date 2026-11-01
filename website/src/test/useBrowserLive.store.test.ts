import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

import { HANDBACK_DRAIN_MS, REPLAY_RINGS_KEPT, TAKEOVER_HEARTBEAT_MS, UNSEEN_FRAMES_BEFORE_PAUSE, browserLiveStore, readBrowserLiveEvent } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'
import { encodeBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * The live-browser store: the state machine a `browser_live` event drives, the
 * one-ack-per-frame rule, the turn binding a card survives a remount with, the
 * visibility OR, and the optimistic claim that reverts when the gateway says no.
 * Every write goes to injected stubs; nothing here touches the network.
 */

const S = 'slot:abc'
const ev = (over: Record<string, unknown> = {}) => ({ session_key: S, turn_id: 't1', state: 'live', url: 'https://jspaint.app/', title: 'untitled - Paint', browser: 'user_chrome', headless: false, steps: [{ label: 'open', status: 'done' }, { label: 'draw', status: 'active' }], step: { index: 1, label: 'draw' }, rung: 0, fps: 58, ...over })
const frame = (seq: number, stream = 's1'): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, stream, seq, w: 1280, h: 800, mask: [] },
  jpeg: new Uint8Array([1, 2, 3]),
  receivedAt: 1000 + seq,
})

let writes: ReturnType<typeof stubWrites>
function stubWrites(claimOk = true) {
  return {
    ack: vi.fn(async () => ({ ok: true })),
    visibility: vi.fn(async () => ({ ok: true })),
    claim: vi.fn(async () => { if (!claimOk) throw new Error('403'); return { ok: true } }),
    release: vi.fn(async () => ({ ok: true })),
    input: vi.fn(async () => ({ ok: true, sent: 1 })),
    open: vi.fn(async () => ({ ok: true })),
    reopen: vi.fn(async (): Promise<unknown> => ({ ok: true })),
    close: vi.fn(async () => ({ ok: true })),
  }
}
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-22T10:00:00Z'))
  browserLiveStore.__resetForTests()
  writes = stubWrites()
  restore = browserLiveStore.__setWritesForTests(writes)
})
afterEach(() => {
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.useRealTimers()
})

describe('readBrowserLiveEvent', () => {
  it('accepts a well-formed event and normalises optional fields', () => {
    const e = readBrowserLiveEvent(ev({ steps: [{ label: 'a', status: 'bogus' }, { nope: 1 }, { label: 'b', status: 'done' }] }))
    expect(e).toMatchObject({ session_key: S, turn_id: 't1', state: 'live', browser: 'user_chrome' })
    expect(e!.steps).toEqual([{ label: 'a', status: 'pending' }, { label: 'b', status: 'done' }])
  })
  it.each([
    ['no session key', ev({ session_key: '' })],
    ['no turn id', ev({ turn_id: undefined })],
    ['unknown state', ev({ state: 'flying' })],
    ['not an object', 'live'],
    ['null', null],
  ])('drops an event with %s', (_l, raw) => {
    expect(readBrowserLiveEvent(raw)).toBeNull()
  })
  it('ignores an unknown browser kind rather than trusting it', () => {
    expect(readBrowserLiveEvent(ev({ browser: 'safari' }))!.browser).toBeUndefined()
  })
})

describe('applyEvent: the state machine', () => {
  it('creates the record on the first event and stamps its start', () => {
    browserLiveStore.applyEvent(ev({ state: 'waiting_allow' }))
    const t = browserLiveStore.current(S)!
    expect(t.state).toBe('waiting_allow')
    expect(t.startedAt).toBe(Date.now())
    expect(t.endedAt).toBeNull()
    expect(t.hasFrame).toBe(false)
  })

  it('a later event for the same turn keeps what it does not restate', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'in_control' })
    const t = browserLiveStore.current(S)!
    expect(t.state).toBe('in_control')
    expect(t.url).toBe('https://jspaint.app/')
    expect(t.steps).toHaveLength(2)
    expect(t.browser).toBe('user_chrome')
  })

  it('done stamps the end once; a repeated done does not move it', () => {
    browserLiveStore.applyEvent(ev())
    vi.advanceTimersByTime(42_000)
    browserLiveStore.applyEvent(ev({ state: 'done', step: undefined }))
    const end = browserLiveStore.current(S)!.endedAt
    expect(end).toBe(Date.now())
    vi.advanceTimersByTime(5_000)
    browserLiveStore.applyEvent(ev({ state: 'done', step: undefined }))
    expect(browserLiveStore.current(S)!.endedAt).toBe(end)
    expect(browserLiveStore.current(S)!.step).toBeNull()
  })

  it('a new turn id closes the previous live turn as done', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyEvent(ev({ turn_id: 't2', state: 'waiting_allow' }))
    expect(browserLiveStore.current(S)!.turnId).toBe('t2')
    // A settled card never binds to a record that is still live.
    expect(browserLiveStore.resolveTurn(S, 'anchor-1', false, Date.now())).toBeNull()
  })

  it('an error event keeps the server detail verbatim', () => {
    browserLiveStore.applyEvent(ev({ state: 'error', detail: 'Chrome closed the debugging port' }))
    expect(browserLiveStore.current(S)).toMatchObject({ state: 'error', detail: 'Chrome closed the debugging port' })
    expect(browserLiveStore.current(S)!.endedAt).not.toBeNull()
  })

  it('a malformed event changes nothing', () => {
    browserLiveStore.applyEvent(ev())
    const listener = vi.fn()
    const off = browserLiveStore.subscribe(listener)
    browserLiveStore.applyEvent({ session_key: S, state: 'live' })
    expect(listener).not.toHaveBeenCalled()
    off()
  })
})

describe('applyFrame: ack once, keep, paint', () => {
  it('acks each new seq exactly once and flips hasFrame on the first frame', () => {
    browserLiveStore.applyEvent(ev())
    const listener = vi.fn()
    browserLiveStore.subscribe(listener)
    browserLiveStore.applyFrame(frame(1))
    browserLiveStore.applyFrame(frame(2))
    browserLiveStore.applyFrame(frame(2))
    expect(writes.ack.mock.calls).toEqual([[S, 1, 's1'], [S, 2, 's1']])
    const t = browserLiveStore.current(S)!
    expect(t.hasFrame).toBe(true)
    expect(t.ring.length).toBe(3)
    // One notify for the first frame only: later frames never re-render React.
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('delivers frames to a subscribed painter, and the last one on subscribe', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyFrame(frame(1))
    const paint = vi.fn()
    const off = browserLiveStore.subscribeFrames(S, 't1', paint)
    expect(paint).toHaveBeenCalledWith(expect.objectContaining({ header: expect.objectContaining({ seq: 1 }) }))
    browserLiveStore.applyFrame(frame(2))
    expect(paint).toHaveBeenCalledTimes(2)
    off()
    browserLiveStore.applyFrame(frame(3))
    expect(paint).toHaveBeenCalledTimes(2)
  })

  it('still acks a frame for a session with no live turn, so the relay never stalls', () => {
    browserLiveStore.applyFrame(frame(9))
    expect(writes.ack).toHaveBeenCalledWith(S, 9, 's1')
    expect(browserLiveStore.current(S)).toBeNull()
  })

  it('a frame after done is acked; only one landing on a turn with none is kept (its thumbnail)', () => {
    browserLiveStore.applyEvent(ev({ state: 'done' }))
    browserLiveStore.applyFrame(frame(1))
    expect(writes.ack).toHaveBeenCalledWith(S, 1, 's1')
    expect(browserLiveStore.current(S)!.ring.length).toBe(1)
    browserLiveStore.applyFrame(frame(2))
    expect(writes.ack).toHaveBeenCalledWith(S, 2, 's1')
    expect(browserLiveStore.current(S)!.ring.length).toBe(1)
  })

  it('the websocket binary branch decodes and lands a frame here', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyBinary(encodeBrowserFrame({ session_key: S, seq: 5, w: 100, h: 50 }, new Uint8Array([1])))
    expect(writes.ack).toHaveBeenCalledWith(S, 5, '')  // a header with no stream id acks the current stream
    expect(browserLiveStore.current(S)!.hasFrame).toBe(true)
    browserLiveStore.applyBinary(new Uint8Array([0, 0]).buffer)
    expect(writes.ack).toHaveBeenCalledTimes(1)
  })
})

describe('resolveTurn: binding a card to a record', () => {
  it('a running turn binds to the current record and keeps it across a remount', () => {
    browserLiveStore.applyEvent(ev())
    expect(browserLiveStore.resolveTurn(S, 'call:a', true, null)!.turnId).toBe('t1')
    browserLiveStore.applyEvent(ev({ turn_id: 't2' }))
    // The first card, remounted after its turn settled, still shows t1.
    expect(browserLiveStore.resolveTurn(S, 'call:a', false, null)!.turnId).toBe('t1')
    expect(browserLiveStore.resolveTurn(S, 'call:a', false, null)!.state).toBe('done')
    // A second running turn's card binds to t2.
    expect(browserLiveStore.resolveTurn(S, 'call:b', true, null)!.turnId).toBe('t2')
  })

  it('a settled, never-bound turn claims the newest unbound record only when the clocks overlap', () => {
    browserLiveStore.applyEvent(ev())
    vi.advanceTimersByTime(30_000)
    browserLiveStore.applyEvent(ev({ state: 'done' }))
    const started = browserLiveStore.current(S)!.startedAt
    // An anchor row from an hour earlier is some other turn's.
    expect(browserLiveStore.resolveTurn(S, 'call:old', false, started - 3_600_000)).toBeNull()
    // One from inside the window is this turn's.
    expect(browserLiveStore.resolveTurn(S, 'call:now', false, started + 1_000)!.turnId).toBe('t1')
    // And once bound, nobody else gets it.
    expect(browserLiveStore.resolveTurn(S, 'call:other', false, started + 2_000)).toBeNull()
  })

  it('returns null for a session the relay never spoke about', () => {
    expect(browserLiveStore.resolveTurn('slot:none', 'call:x', true, null)).toBeNull()
  })
})

describe('visibility: the OR of every card instance showing the current turn, and the document', () => {
  it('reports true on the first visible instance and false when the last leaves', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.setVisible(S, 'card-1', true, 't1')
    browserLiveStore.setVisible(S, 'card-2', true, 't1')
    browserLiveStore.setVisible(S, 'card-1', false, 't1')
    expect(writes.visibility.mock.calls).toEqual([[S, true]])
    browserLiveStore.setVisible(S, 'card-2', false, 't1')
    expect(writes.visibility.mock.calls).toEqual([[S, true], [S, false]])
  })

  it('a completed card still on screen does not keep a hidden later turn streaming', () => {
    // Turn 1 ends; its folded card stays on screen in the transcript.
    browserLiveStore.applyEvent(ev())
    browserLiveStore.setVisible(S, 'card-1', true, 't1')
    browserLiveStore.applyEvent(ev({ state: 'done' }))
    // Turn 2 starts; its card mounts off screen (scrolled away) and says so.
    browserLiveStore.applyEvent(ev({ turn_id: 't2' }))
    browserLiveStore.setVisible(S, 'card-2', false, 't2')
    expect(writes.visibility.mock.calls).toEqual([[S, true], [S, false]])
    // Frames for turn 2 are acked but the gateway was told nobody is watching;
    // the old card's instance does not count for the new turn.
    for (let i = 1; i <= UNSEEN_FRAMES_BEFORE_PAUSE + 5; i++) browserLiveStore.applyFrame(frame(i))
    expect(writes.visibility.mock.calls).toEqual([[S, true], [S, false]])
    // The new card scrolls into view: streaming resumes.
    browserLiveStore.setVisible(S, 'card-2', true, 't2')
    expect(writes.visibility.mock.calls.at(-1)).toEqual([S, true])
  })

  it("a new turn whose card never mounts is paused by its own frames, not kept alive by the old card", () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.setVisible(S, 'card-1', true, 't1')
    browserLiveStore.applyEvent(ev({ turn_id: 't2' }))
    for (let i = 1; i < UNSEEN_FRAMES_BEFORE_PAUSE; i++) browserLiveStore.applyFrame(frame(i))
    expect(writes.visibility.mock.calls).toEqual([[S, true]])
    browserLiveStore.applyFrame(frame(UNSEEN_FRAMES_BEFORE_PAUSE))
    expect(writes.visibility.mock.calls).toEqual([[S, true], [S, false]])
  })

  it('a hidden document reads as not visible, and a return re-reports', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.setVisible(S, 'card-1', true, 't1')
    const spy = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(writes.visibility.mock.calls.at(-1)).toEqual([S, false])
    spy.mockReturnValue('visible')
    document.dispatchEvent(new Event('visibilitychange'))
    expect(writes.visibility.mock.calls.at(-1)).toEqual([S, true])
    spy.mockRestore()
  })
})

describe('claim and release', () => {
  it('while held and on screen, visibility is re-posted so a static page does not read as a closed card', async () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.setVisible(S, 'card-1', true, 't1')
    expect(await browserLiveStore.claim(S)).toBe(true)
    writes.visibility.mockClear()
    vi.advanceTimersByTime(TAKEOVER_HEARTBEAT_MS)
    expect(writes.visibility.mock.calls).toEqual([[S, true]])
    // Off screen: nothing is claimed on the person's behalf.
    browserLiveStore.setVisible(S, 'card-1', false, 't1')
    writes.visibility.mockClear()
    vi.advanceTimersByTime(TAKEOVER_HEARTBEAT_MS)
    expect(writes.visibility).not.toHaveBeenCalled()
    // Handed back: the heartbeat stops.
    browserLiveStore.setVisible(S, 'card-1', true, 't1')
    expect(await browserLiveStore.release(S)).toBe(true)
    writes.visibility.mockClear()
    vi.advanceTimersByTime(TAKEOVER_HEARTBEAT_MS * 3)
    expect(writes.visibility).not.toHaveBeenCalled()
  })

  it('claim flips to in_control at once and posts the claim', async () => {
    browserLiveStore.applyEvent(ev())
    const p = browserLiveStore.claim(S)
    expect(browserLiveStore.inControl(S)).toBe(true)
    await expect(p).resolves.toBe(true)
    expect(writes.claim).toHaveBeenCalledWith(S)
  })

  it('a refused claim reverts to the state before it and keeps the refusal for the notice', async () => {
    browserLiveStore.__setWritesForTests(stubWrites(false))
    browserLiveStore.applyEvent(ev({ state: 'login_needed' }))
    await expect(browserLiveStore.claim(S)).resolves.toBe(false)
    expect(browserLiveStore.current(S)!.state).toBe('login_needed')
    expect(browserLiveStore.current(S)!.controlError).toBe('403')
    // A tick event in the same state keeps the notice; the server moving the state clears it.
    browserLiveStore.applyEvent(ev({ state: 'login_needed', fps: 12 }))
    expect(browserLiveStore.current(S)!.controlError).toBe('403')
    browserLiveStore.applyEvent(ev({ state: 'live' }))
    expect(browserLiveStore.current(S)!.controlError).toBe('')
  })

  it('claim does nothing on a finished turn', async () => {
    browserLiveStore.applyEvent(ev({ state: 'done' }))
    await expect(browserLiveStore.claim(S)).resolves.toBe(false)
    expect(writes.claim).not.toHaveBeenCalled()
  })

  it('release flips back to live and posts; a release while not in control is a no-op', async () => {
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.release(S)
    expect(writes.release).not.toHaveBeenCalled()
    await browserLiveStore.claim(S)
    await browserLiveStore.release(S)
    expect(browserLiveStore.current(S)!.state).toBe('live')
    expect(writes.release).toHaveBeenCalledWith(S)
  })

  it('a hand-back waits for the canvases to send their releases, and only so long', async () => {
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.claim(S)
    let sent: () => void = () => {}
    const drain = vi.fn(() => new Promise<void>((resolve) => { sent = resolve }))
    const off = browserLiveStore.onHandBack(S, drain)
    const done = browserLiveStore.release(S)
    expect(drain).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10)
    expect(writes.release).not.toHaveBeenCalled()
    sent()
    await expect(done).resolves.toBe(true)
    expect(writes.release).toHaveBeenCalledTimes(1)

    // A chain stuck behind a slow POST holds the hand-back for HANDBACK_DRAIN_MS at most.
    await browserLiveStore.claim(S)
    drain.mockImplementation(() => new Promise<void>(() => {}))
    const stuck = browserLiveStore.release(S)
    await vi.advanceTimersByTimeAsync(HANDBACK_DRAIN_MS - 1)
    expect(writes.release).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    await expect(stuck).resolves.toBe(true)
    expect(writes.release).toHaveBeenCalledTimes(2)

    // Unregistered, the hand-back goes at once.
    off()
    await browserLiveStore.claim(S)
    await browserLiveStore.release(S)
    expect(drain).toHaveBeenCalledTimes(2)
    expect(writes.release).toHaveBeenCalledTimes(3)
  })

  it('input and open-in-browser go straight to their writes', async () => {
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.sendInput(S, { type: 'key', action: 'down', key: 'a', code: 'KeyA', text: 'a', modifiers: 0 })
    expect(writes.input).toHaveBeenCalledWith(S, expect.objectContaining({ type: 'key', key: 'a' }))
    browserLiveStore.openInBrowser(S)
    expect(writes.open).toHaveBeenCalledWith(S)
  })
})

describe('acks are scoped to the stream', () => {
  it('a new stream numbering from 1 is acked from 1, not held behind the old high-water mark', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyFrame(frame(100, 'old'))
    browserLiveStore.applyEvent(ev({ turn_id: 't2' }))
    browserLiveStore.applyFrame(frame(1, 'new'))
    browserLiveStore.applyFrame(frame(1, 'new'))
    browserLiveStore.applyFrame(frame(2, 'new'))
    expect(writes.ack.mock.calls).toEqual([[S, 100, 'old'], [S, 1, 'new'], [S, 2, 'new']])
  })

  it('a same-turn tab switch (new stream, same turn id) resets the mark too', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.applyFrame(frame(50, 'tab-a'))
    browserLiveStore.applyFrame(frame(1, 'tab-b'))
    expect(writes.ack.mock.calls).toEqual([[S, 50, 'tab-a'], [S, 1, 'tab-b']])
  })
})

describe('replay rings are bounded per session', () => {
  it(`keeps the rings of the newest ${REPLAY_RINGS_KEPT} turns and empties the rest`, () => {
    for (let i = 1; i <= REPLAY_RINGS_KEPT + 2; i++) {
      browserLiveStore.applyEvent(ev({ turn_id: `t${i}` }))
      browserLiveStore.applyFrame(frame(1, `s${i}`))
      browserLiveStore.applyEvent(ev({ turn_id: `t${i}`, state: 'done' }))
    }
    // Records survive; only the old frames are gone.
    expect(browserLiveStore.current(S)!.ring.length).toBe(1)
    const store = browserLiveStore as unknown as { sessions: Map<string, { turns: Map<string, { ring: { length: number } }> }> }
    const rings = [...store.sessions.get(S)!.turns.values()].map(t => t.ring.length)
    expect(rings).toEqual([0, 0, 1, 1])
    // An emptied turn no longer claims a frame, so its card asks for the recorded thumbnail.
    expect(browserLiveStore.sessionTurns(S).map(t => t.hasFrame)).toEqual([false, false, true, true])
  })
})

describe('a session no card shows', () => {
  it(`is reported hidden after ${UNSEEN_FRAMES_BEFORE_PAUSE} unseen frames, and shown again when a card mounts`, () => {
    browserLiveStore.applyEvent(ev())
    for (let i = 1; i < UNSEEN_FRAMES_BEFORE_PAUSE; i++) browserLiveStore.applyFrame(frame(i))
    expect(writes.visibility).not.toHaveBeenCalled()
    browserLiveStore.applyFrame(frame(UNSEEN_FRAMES_BEFORE_PAUSE))
    expect(writes.visibility.mock.calls).toEqual([[S, false]])
    browserLiveStore.setVisible(S, 'card-1', true, 't1')
    expect(writes.visibility.mock.calls).toEqual([[S, false], [S, true]])
  })

  it('a card that reported first is never second-guessed by frames', () => {
    browserLiveStore.applyEvent(ev())
    browserLiveStore.setVisible(S, 'card-1', true, 't1')
    for (let i = 1; i <= UNSEEN_FRAMES_BEFORE_PAUSE * 2; i++) browserLiveStore.applyFrame(frame(i))
    expect(writes.visibility.mock.calls).toEqual([[S, true]])
  })
})

describe('a hand back the gateway refuses', () => {
  it('puts the card back in control and keeps the gateway text for the notice', async () => {
    writes.release.mockImplementation(async () => { throw new Error('the live view is not accepting input') })
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.claim(S)
    await expect(browserLiveStore.release(S)).resolves.toBe(false)
    const t = browserLiveStore.current(S)!
    expect(t.state).toBe('in_control')
    expect(t.controlError).toBe('the live view is not accepting input')
    // The server confirming the lock is free clears it; while it says in_control it stays.
    browserLiveStore.applyEvent(ev({ state: 'in_control' }))
    expect(browserLiveStore.current(S)!.controlError).toBe('the live view is not accepting input')
    browserLiveStore.applyEvent(ev({ state: 'live' }))
    expect(browserLiveStore.current(S)!.controlError).toBe('')
  })

  it('a successful hand back after a failed one clears the notice', async () => {
    let fail = true
    writes.release.mockImplementation(async () => { if (fail) throw new Error('409'); return { ok: true } })
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.claim(S)
    await browserLiveStore.release(S)
    expect(browserLiveStore.current(S)!.controlError).toBe('409')
    fail = false
    await expect(browserLiveStore.release(S)).resolves.toBe(true)
    expect(browserLiveStore.current(S)!.state).toBe('live')
    expect(browserLiveStore.current(S)!.controlError).toBe('')
  })

  it('a refused input while in control is surfaced the same way', async () => {
    writes.input.mockImplementation(async () => { throw new Error('no_stream') })
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.claim(S)
    await browserLiveStore.sendInput(S, { type: 'key', action: 'down', key: 'a', code: 'KeyA', text: 'a', modifiers: 0 })
    expect(browserLiveStore.current(S)!.state).toBe('in_control')
    expect(browserLiveStore.current(S)!.controlError).toBe('no_stream')
  })

  it('an input counts as sent only when the gateway says it dispatched it, and never rejects', async () => {
    browserLiveStore.applyEvent(ev())
    await browserLiveStore.claim(S)
    const key = { type: 'key', action: 'down', key: 'a', code: 'KeyA', text: 'a', modifiers: 0 } as const
    writes.input.mockImplementationOnce(async () => ({ sent: 2, focus: { editable: false } }))
    await expect(browserLiveStore.sendInput(S, key)).resolves.toEqual({ sent: true, reply: { sent: 2, focus: { editable: false } } })
    writes.input.mockImplementationOnce(async () => ({ sent: 0 }))
    await expect(browserLiveStore.sendInput(S, key)).resolves.toMatchObject({ sent: false })
    writes.input.mockImplementationOnce(async () => ({ ok: true }))
    await expect(browserLiveStore.sendInput(S, key)).resolves.toMatchObject({ sent: false })
    writes.input.mockImplementationOnce(async () => { throw new Error('net') })
    await expect(browserLiveStore.sendInput(S, key)).resolves.toEqual({ sent: false, reply: undefined })
  })
})

describe('reopen: the finished card goes live again', () => {
  it('a live event after done on the same turn is the reopened view; its end time is kept', () => {
    browserLiveStore.applyEvent(ev())
    vi.advanceTimersByTime(5000)
    browserLiveStore.applyEvent(ev({ state: 'done', turn_over: true, tab: 'open' }))
    const ended = browserLiveStore.current(S)!.endedAt
    expect(ended).toBe(Date.now())
    vi.advanceTimersByTime(60_000)
    browserLiveStore.applyEvent(ev({ state: 'live', turn_over: true, tab: 'open' }))
    const t = browserLiveStore.current(S)!
    expect(t.turnId).toBe('t1')
    expect(t.state).toBe('live')
    expect(t.turnOver).toBe(true)
    expect(t.endedAt).toBe(ended)
    // Frames reach the reopened view (an ended record drops them).
    browserLiveStore.applyFrame(frame(1))
    expect(browserLiveStore.current(S)!.hasFrame).toBe(true)
  })

  it('posts the turn id; tab_gone marks the tab closed instead of an error', async () => {
    browserLiveStore.applyEvent(ev({ state: 'done', turn_over: true, tab: 'open' }))
    expect(await browserLiveStore.reopen(S, 't1')).toBe(true)
    expect(writes.reopen).toHaveBeenCalledWith(S, 't1')
    writes.reopen.mockRejectedValueOnce(Object.assign(new Error('the tab this card showed is closed'), { status: 409, body: '{"code":"tab_gone","error":"x"}' }))
    expect(await browserLiveStore.reopen(S, 't1')).toBe(false)
    expect(browserLiveStore.current(S)!.tab).toBe('closed')
    expect(browserLiveStore.current(S)!.controlError).toBe('')
    writes.reopen.mockRejectedValueOnce(Object.assign(new Error('a newer browser card belongs to this chat'), { status: 409, body: '{"code":"stale_turn"}' }))
    await browserLiveStore.reopen(S, 't1')
    expect(browserLiveStore.current(S)!.controlError).toBe('a newer browser card belongs to this chat')
  })

  it('a running turn\'s card never binds a reopened record that belongs to an earlier card', () => {
    browserLiveStore.applyEvent(ev())
    expect(browserLiveStore.resolveTurn(S, 'call:1', true, null)!.turnId).toBe('t1')
    browserLiveStore.applyEvent(ev({ state: 'done', turn_over: true, tab: 'open' }))
    browserLiveStore.applyEvent(ev({ state: 'live', turn_over: true, tab: 'open' }))
    expect(browserLiveStore.resolveTurn(S, 'call:2', true, null)).toBeNull()
    // The next turn's own event is what that card binds to.
    browserLiveStore.applyEvent(ev({ turn_id: 't2', state: 'live', turn_over: false }))
    expect(browserLiveStore.resolveTurn(S, 'call:2', true, null)!.turnId).toBe('t2')
    expect(browserLiveStore.isCurrentTurn(S, 't1')).toBe(false)
  })

  it('closeLive posts the close', () => {
    browserLiveStore.closeLive(S)
    expect(writes.close).toHaveBeenCalledWith(S)
  })
})

describe('card actions that fail', () => {
  it('a refused Open in browser or Close live is kept as the control error', async () => {
    browserLiveStore.applyEvent(ev())
    writes.open.mockRejectedValueOnce(new Error('open refused'))
    await browserLiveStore.openInBrowser(S)
    expect(browserLiveStore.current(S)?.controlError).toContain('open refused')
    writes.close.mockRejectedValueOnce(new Error('close refused'))
    await browserLiveStore.closeLive(S)
    expect(browserLiveStore.current(S)?.controlError).toContain('close refused')
  })
})
