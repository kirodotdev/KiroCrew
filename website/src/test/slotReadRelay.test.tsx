/**
 * Cross-window unread-badge sync: the `slot_read` relay.
 *
 * Read marks were window-local (Redux + localStorage), so reading a session
 * in one dashboard window left the sidebar bubble lit in every other one.
 * These specs pin the three legs of the fix:
 *
 *  - the relay module's per-slot throttle (leading send, one coalesced
 *    trailing send, never a dropped final read),
 *  - the socket wiring: an inbound `slot_read` frame clears the local badge
 *    and never echoes back out; the arrival branch relays a read only for
 *    this window's visible active slot,
 *  - the read-gesture sites: `switchSlot` relays the slot it just read.
 *
 * Harness mirrors UseWebSocketCoverage: the hook dispatches through the
 * Provider store but reads `activeSlot` off the singleton store, so tests
 * prime both and reset both.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import chatReducer, { setActiveSlot, clearMessages, switchSlot } from '../store/chatSlice'
import dashboardReducer, { addSlotOptimistic, removeSlotOptimistic, updateSlot, markSlotRead, markSlotUnread, remoteSlotRead, restoreUnreadSince, restoreUnreadBadges, MANUAL_UNREAD } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import { bindSlotReadSender, emitSlotRead, flushSlotRead, _resetSlotReadRelayForTest } from '../lib/slotReadRelay'

/** Flip jsdom's document.hidden and fire the visibilitychange the hook listens for. */
const setDocumentHidden = (v: boolean) => {
  Object.defineProperty(document, 'hidden', { value: v, configurable: true })
  document.dispatchEvent(new Event('visibilitychange'))
}
/** Occlusion/unfocus mock: Page Visibility still says "visible" for a window
 *  parked behind other apps — only hasFocus() tells them apart. */
const setDocumentFocused = (v: boolean) => {
  Object.defineProperty(document, 'hasFocus', { value: () => v, configurable: true })
}

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    autonudgeList: vi.fn().mockResolvedValue({ enabled: false, loops: [] }),
    monitorsList: vi.fn().mockResolvedValue({ enabled: false, monitors: [] }),
    pendingQuestions: vi.fn().mockResolvedValue([]),
    sessions: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
  },
}))

const ACTIVE = 'slot-active'
const BACKGROUND = 'slot-background'

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn(() => { this.readyState = MockWebSocket.CLOSED })
  constructor(public url: string) { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: unknown) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

describe('slotReadRelay module throttle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    _resetSlotReadRelayForTest()
  })
  afterEach(() => {
    _resetSlotReadRelayForTest()
    vi.useRealTimers()
  })

  it('sends the first read immediately (leading edge)', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')
    expect(sent).toEqual(['k1'])
  })

  it('coalesces a burst into exactly one trailing send carrying the newest watermark', () => {
    const sent: Array<[string, string | undefined]> = []
    bindSlotReadSender((s, ts) => sent.push([s, ts]))
    emitSlotRead('k1', '2026-01-01T00:00:01Z')
    emitSlotRead('k1', '2026-01-01T00:00:03Z')
    emitSlotRead('k1', '2026-01-01T00:00:02Z')
    expect(sent).toEqual([['k1', '2026-01-01T00:00:01Z']])   // burst suppressed…
    vi.advanceTimersByTime(1_000)
    // …but the LAST read still lands, watermarked at the NEWEST ts seen.
    expect(sent).toEqual([['k1', '2026-01-01T00:00:01Z'], ['k1', '2026-01-01T00:00:03Z']])
    vi.advanceTimersByTime(5_000)
    expect(sent.length).toBe(2)           // trailing send does not self-perpetuate
  })

  it('a quiet window with no repeat sends nothing at its end', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')
    vi.advanceTimersByTime(5_000)
    expect(sent).toEqual(['k1'])
  })

  it('throttles per slot, not globally', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')
    emitSlotRead('k2')
    expect(sent).toEqual(['k1', 'k2'])
  })

  it('is a safe no-op unbound and for an empty key', () => {
    expect(() => emitSlotRead('k1')).not.toThrow()
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('')
    expect(sent).toEqual([])
  })

  it('flush sends a pending trailing relay immediately and disarms its timer', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')
    emitSlotRead('k1')          // leading sent + trailing pending
    flushSlotRead('k1')
    expect(sent).toEqual(['k1', 'k1'])
    vi.advanceTimersByTime(5_000)
    expect(sent).toEqual(['k1', 'k1'])  // timer disarmed: no third send
  })

  it('flush of a quiet window sends nothing extra', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1')          // leading only, no repeat
    flushSlotRead('k1')
    vi.advanceTimersByTime(5_000)
    expect(sent).toEqual(['k1'])
  })

  it('a targeted flush leaves other slots pending', () => {
    const sent: string[] = []
    bindSlotReadSender(s => sent.push(s))
    emitSlotRead('k1'); emitSlotRead('k1')
    emitSlotRead('k2'); emitSlotRead('k2')
    flushSlotRead('k1')
    expect(sent).toEqual(['k1', 'k2', 'k1'])
    vi.advanceTimersByTime(1_000)
    expect(sent).toEqual(['k1', 'k2', 'k1', 'k2'])  // k2 trailing untouched
  })
})

describe('slot_read over the dashboard socket', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    _resetSlotReadRelayForTest()
    WS_INSTANCES.length = 0
    testStore = createTestStore({
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE },
    })
    vi.stubGlobal('WebSocket', MockWebSocket)
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    _resetSlotReadRelayForTest()
    setDocumentHidden(false)
    vi.unstubAllGlobals()
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mount() {
    const hook = renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { ...hook, ws }
  }

  const dash = () => testStore.getState().dashboard
  const sentReadFrames = (ws: MockWebSocket) =>
    ws.send.mock.calls.map(c => c[0] as string).filter(f => f.includes('"slot_read"'))

  it('an inbound slot_read frame retires a badge its watermark covers', () => {
    const { ws } = mount()
    testStore.dispatch(markSlotUnread({ slot: BACKGROUND, ts: '2026-01-01T00:00:05Z' }))
    expect(dash().unreadSlots).toContain(BACKGROUND)
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: BACKGROUND, read_ts: '2026-01-01T00:00:09Z' } }) })
    expect(dash().unreadSlots).not.toContain(BACKGROUND)
  })

  it('an inbound slot_read older than the badge keeps it lit (watermark)', () => {
    const { ws } = mount()
    // The F1 race: A read message N (ts 5) and relayed; N+1 (ts 9) badged this
    // window before the relay landed. The stale relay must not clear N+1.
    testStore.dispatch(markSlotUnread({ slot: BACKGROUND, ts: '2026-01-01T00:00:09Z' }))
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: BACKGROUND, read_ts: '2026-01-01T00:00:05Z' } }) })
    expect(dash().unreadSlots).toContain(BACKGROUND)
  })

  it('a manual mark-as-unread is never cleared by a remote read', () => {
    const { ws } = mount()
    testStore.dispatch(markSlotUnread(BACKGROUND))   // string form = manual reminder
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: BACKGROUND, read_ts: '2099-12-31T23:59:59Z' } }) })
    expect(dash().unreadSlots).toContain(BACKGROUND)
  })

  it('a boot-restored badge (no watermark recorded) accepts any relayed read', () => {
    // unreadSince is deliberately not persisted, so a badge restored from
    // localStorage has no watermark: any relayed read clears it, even one
    // with no read_ts of its own.
    const boot = { ...dashboardReducer(undefined, { type: '@@INIT' }), unreadSlots: ['restored-slot'] }
    const cleared = dashboardReducer(boot, remoteSlotRead({ slot: 'restored-slot', readTs: undefined }))
    expect(cleared.unreadSlots).not.toContain('restored-slot')
  })

  it('an inbound slot_read never echoes back out (no relay loop)', () => {
    const { ws } = mount()
    testStore.dispatch(markSlotUnread({ slot: BACKGROUND, ts: '2026-01-01T00:00:05Z' }))
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: BACKGROUND, read_ts: '2026-01-01T00:00:09Z' } }) })
    expect(sentReadFrames(ws)).toEqual([])
  })

  it('a malformed slot_read frame is ignored', () => {
    const { ws } = mount()
    testStore.dispatch(markSlotUnread({ slot: BACKGROUND, ts: '2026-01-01T00:00:05Z' }))
    act(() => { ws.simulateMessage({ type: 'slot_read', data: {} }) })
    act(() => { ws.simulateMessage({ type: 'slot_read', data: { slot: 42 } }) })
    expect(dash().unreadSlots).toContain(BACKGROUND)
  })

  it('a message landing in the visible active slot relays a read', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
    })
    expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:00Z' })])
  })

  it('a message landing in the active slot on a NON-chat route relays nothing', () => {
    // chat.activeSlot survives navigating to Settings; a visible tab there
    // must not broadcast a read for a transcript it is not rendering.
    const { ws } = mount()
    window.history.pushState({}, '', '/settings')
    try {
      act(() => {
        ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
      })
      expect(sentReadFrames(ws)).toEqual([])
    } finally {
      window.history.pushState({}, '', '/')
    }
  })

  it('a message landing in a background slot badges it and relays nothing', () => {
    const { ws } = mount()
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: BACKGROUND, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
    })
    expect(dash().unreadSlots).toContain(BACKGROUND)
    expect(sentReadFrames(ws)).toEqual([])
  })

  it('switchSlot relays the read of the slot it opens', async () => {
    const { ws } = mount()
    await act(async () => { await testStore.dispatch(switchSlot(BACKGROUND) as never) })
    // No slot metadata in this store, so no last_ts exists: the relay goes out
    // WITHOUT a watermark rather than minting client time (receivers then
    // apply their conservative default).
    const frames = sentReadFrames(ws).map(f => JSON.parse(f) as { type: string; slot: string; read_ts?: string })
    expect(frames.some(f => f.slot === BACKGROUND && f.read_ts === undefined)).toBe(true)
  })

  it('switching away flushes the outgoing slot\'s pending trailing relay', () => {
    const { ws } = mount()
    act(() => {
      // Two arrivals in the visible active slot: leading frame + pending trailing.
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'a', ts: '2026-09-10T00:00:00Z' } })
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'b', ts: '2026-09-10T00:00:01Z' } })
    })
    expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:00Z' })])
    // The slot stops being visible-active: the coalesced trailing read goes
    // out NOW (watermarked at the newest arrival), so no timer survives to
    // wipe a later re-badge.
    act(() => { globalStore.dispatch(setActiveSlot(BACKGROUND)) })
    expect(sentReadFrames(ws)).toEqual([
      JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:00Z' }),
      JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:01Z' }),
    ])
  })

  it('reveal relays the slot that is active NOW, never a stale hidden arrival', () => {
    const { ws } = mount()
    act(() => { setDocumentHidden(true) })
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
    })
    act(() => { globalStore.dispatch(setActiveSlot(BACKGROUND)) })
    act(() => { setDocumentHidden(false) })
    // The reveal read names what the user now sees. This store holds no slot
    // metadata, so no last_ts exists: the frame goes out without a watermark
    // rather than minting client time (receivers apply their conservative
    // default and keep watermarked badges lit).
    const frames = sentReadFrames(ws).map(f => JSON.parse(f) as { slot: string; read_ts?: string })
    expect(frames.length).toBe(1)
    expect(frames[0].slot).toBe(BACKGROUND)
    expect(frames[0].read_ts).toBeUndefined()
  })

  it('markSlotRead is a persistence no-op for a key that is not unread', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem')
    const before = dashboardReducer(undefined, { type: '@@INIT' })
    spy.mockClear()
    const after = dashboardReducer(before, markSlotRead('never-unread'))
    expect(after.unreadSlots).toEqual(before.unreadSlots)
    expect(spy).not.toHaveBeenCalled()             // echo fan-in writes nothing
    spy.mockRestore()
  })

  it('a message-lit badge without any actual ts accepts any relayed read', () => {
    // No frame ts and no slot last_ts: nothing is recorded (client time is
    // never minted), so a relayed read — even watermark-less — clears it.
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    st = dashboardReducer(st, markSlotUnread({ slot: 'no-ts-slot' }))
    expect(st.unreadSince['no-ts-slot']).toBeUndefined()
    st = dashboardReducer(st, remoteSlotRead({ slot: 'no-ts-slot', readTs: undefined }))
    expect(st.unreadSlots).not.toContain('no-ts-slot')
  })

  it('compares watermarks as instants, not strings (mixed-offset timestamps)', () => {
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    // since = 00:00Z written as +02:00; a read at 01:00Z covers it even though
    // the lexical comparison would say otherwise.
    st = dashboardReducer(st, markSlotUnread({ slot: 'tz-slot', ts: '2026-01-01T02:00:00+02:00' }))
    st = dashboardReducer(st, remoteSlotRead({ slot: 'tz-slot', readTs: '2026-01-01T01:00:00Z' }))
    expect(st.unreadSlots).not.toContain('tz-slot')
    // …and an unparseable watermark can never clear a badge.
    st = dashboardReducer(st, markSlotUnread({ slot: 'tz-slot', ts: '2026-01-01T05:00:00Z' }))
    st = dashboardReducer(st, remoteSlotRead({ slot: 'tz-slot', readTs: 'not-a-timestamp' }))
    expect(st.unreadSlots).toContain('tz-slot')
  })

  it('a manual reminder survives reload: the sentinel is persisted and restored', () => {
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    st = dashboardReducer(st, markSlotUnread('remember-me'))
    const persisted = JSON.parse(sessionStorage.getItem('mc-unread-since') ?? '{}') as Record<string, string>
    expect(persisted['remember-me']).toBe(MANUAL_UNREAD)  // written at mark time
    // GPT F2 regression (span cf2f388263a4, head a30c8e312): the manual badge
    // never publishes to the shared store — a sibling window's boot must not
    // surface this window's private reminder.
    expect((JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)['remember-me']).toBeUndefined()
    expect(JSON.parse(localStorage.getItem('mc-unread-slots') ?? '[]') as string[]).not.toContain('remember-me')
    dashboardReducer(st, markSlotRead('remember-me'))
    const cleared = JSON.parse(sessionStorage.getItem('mc-unread-since') ?? '{}') as Record<string, string>
    expect(cleared['remember-me']).toBeUndefined()        // pruned on local read
    // The store is per-window (sessionStorage), so a sibling window writing its
    // own watermark map cannot clobber this one: nothing lands in the shared
    // localStorage at all.
    expect(localStorage.getItem('mc-unread-since')).toBeNull()
  })

  it('a message watermark survives reload AND a fresh tab: a stale relay cannot erase the shared badge', () => {
    // GPT F1 regressions (spans cf2f388263a4 / R15 UPHOLD-FENCED): window B
    // badges N+1 at 00:05. A window that boots — B reloading OR a brand-new
    // tab whose sessionStorage is EMPTY — restores the badge from the shared
    // list WITH its watermark from the shared map, so window A's stale relay
    // for N (readTs 00:03) neither clears the badge nor erases the shared key.
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    st = dashboardReducer(st, markSlotUnread({ slot: 'reload-slot', ts: '2026-01-01T00:05:00Z' }))
    expect(st.unreadSince['reload-slot']).toBe('2026-01-01T00:05:00Z')
    // Badge and watermark landed together in the ONE shared record.
    expect((JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)['reload-slot']).toBe('2026-01-01T00:05:00Z')
    // FRESH TAB: per-tab sessionStorage is empty; boot from shared state only.
    sessionStorage.clear()
    const restoredBadges = JSON.parse(localStorage.getItem('mc-unread-slots') ?? '[]') as string[]
    expect(restoredBadges).toContain('reload-slot')                // badge restored
    const restoredSince = restoreUnreadSince()
    expect(restoredSince['reload-slot']).toBe('2026-01-01T00:05:00Z')  // WITH its watermark
    const rebooted = { ...dashboardReducer(undefined, { type: '@@INIT' }), unreadSlots: restoredBadges, unreadSince: restoredSince }
    // The stale relay for the older message clears nothing, locally or shared...
    let after = dashboardReducer(rebooted, remoteSlotRead({ slot: 'reload-slot', readTs: '2026-01-01T00:03:00Z' }))
    expect(after.unreadSlots).toContain('reload-slot')
    expect(JSON.parse(localStorage.getItem('mc-unread-slots') ?? '[]')).toContain('reload-slot')
    // ...and a covering relay still clears badge and watermark together.
    after = dashboardReducer(after, remoteSlotRead({ slot: 'reload-slot', readTs: '2026-01-01T00:06:00Z' }))
    expect(after.unreadSlots).not.toContain('reload-slot')
    expect((JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)['reload-slot']).toBeUndefined()
  })

  it('persisting is a per-slot delta: sibling-window keys survive every write site', () => {
    const stored = () => Object.keys(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    // Slot metadata so a local read can PROVE coverage of the shared
    // watermark (shared clears are guarded by the slot's last_ts).
    st = dashboardReducer(st, addSlotOptimistic({ key: 'mine-X', last_ts: '2026-01-01T00:00:59Z' } as never))
    // A sibling window persisted Y; this window's memory has never seen it.
    localStorage.setItem('mc-unread-shared', JSON.stringify({ 'sibling-Y': '2026-01-01T00:00:02Z' }))
    st = dashboardReducer(st, markSlotUnread({ slot: 'mine-X', ts: '2026-01-01T00:00:01Z' }))
    expect(stored().sort()).toEqual(['mine-X', 'sibling-Y'])   // add composes
    st = dashboardReducer(st, remoteSlotRead({ slot: 'mine-X', readTs: '2026-01-01T00:00:09Z' }))
    expect(stored()).toEqual(['sibling-Y'])                    // remote clear removes only its slot
    st = dashboardReducer(st, markSlotUnread({ slot: 'mine-X', ts: '2026-01-01T00:00:10Z' }))
    st = dashboardReducer(st, markSlotRead('mine-X'))
    expect(stored()).toEqual(['sibling-Y'])                    // local read removes only its slot
    st = dashboardReducer(st, markSlotUnread({ slot: 'mine-X', ts: '2026-01-01T00:00:11Z' }))
    dashboardReducer(st, removeSlotOptimistic('mine-X'))
    expect(stored()).toEqual(['sibling-Y'])                    // slot removal removes only its slot
  })

  it('a stale window cannot delete a sibling\'s newer shared watermark: the clear is guarded by the SHARED value', () => {
    // GPT F1 regression (R16 UPHOLD-FENCED): window A's local watermark is
    // stale (reconnect gap) at 00:02; sibling window B advanced the SHARED
    // watermark to 00:08. A relay at 00:05 covers A's stale local value but
    // NOT B's shared one — before the fix A cleared shared state
    // unconditionally, silently erasing B's newer badge globally.
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    st = dashboardReducer(st, markSlotUnread({ slot: 'stale-slot', ts: '2026-01-01T00:02:00Z' }))
    // Sibling B advances the SHARED watermark past this window's view.
    localStorage.setItem('mc-unread-shared', JSON.stringify({
      ...(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>),
      'stale-slot': '2026-01-01T00:08:00Z',
    }))
    // Relay covers the stale local watermark (00:02) but not shared (00:08).
    st = dashboardReducer(st, remoteSlotRead({ slot: 'stale-slot', readTs: '2026-01-01T00:05:00Z' }))
    // Shared state survives...
    expect((JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)['stale-slot']).toBe('2026-01-01T00:08:00Z')
    expect(JSON.parse(localStorage.getItem('mc-unread-slots') ?? '[]')).toContain('stale-slot')
    // ...and this window ADOPTS the newer watermark, keeping the badge lit.
    expect(st.unreadSince['stale-slot']).toBe('2026-01-01T00:08:00Z')
    expect(st.unreadSlots).toContain('stale-slot')
    // A relay covering the shared watermark clears everything, everywhere.
    st = dashboardReducer(st, remoteSlotRead({ slot: 'stale-slot', readTs: '2026-01-01T00:09:00Z' }))
    expect((JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)['stale-slot']).toBeUndefined()
    expect(JSON.parse(localStorage.getItem('mc-unread-slots') ?? '[]')).not.toContain('stale-slot')
    expect(st.unreadSlots).not.toContain('stale-slot')
  })

  it('first boot after upgrade seeds the record from the legacy badge list; a manual reminder re-seeds per-tab', () => {
    // Older code persisted badges as a bare list with no watermarks. The
    // first boot that finds NO record seeds one entry per legacy badge with
    // '' — the badge survives the upgrade, and any relayed read clears it
    // (no watermark ever guarded it). The per-tab manual sentinel M still
    // re-seeds its badge into THIS window's list without writing shared state.
    localStorage.removeItem('mc-unread-shared')
    localStorage.setItem('mc-unread-slots', JSON.stringify(['legacy-L']))
    sessionStorage.setItem('mc-unread-since', JSON.stringify({ 'manual-M': MANUAL_UNREAD }))
    const restored = restoreUnreadSince()
    expect(restored['legacy-L']).toBeUndefined()          // '' = no watermark recorded
    expect(restored['manual-M']).toBe(MANUAL_UNREAD)      // per-tab sentinel joins
    expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual({ 'legacy-L': '' })
    expect(restoreUnreadBadges(restored).sort()).toEqual(['legacy-L', 'manual-M'])
    // The seeded badge accepts any relayed read — and the clear removes the
    // record entry, badge and (absent) watermark together.
    const rebooted = { ...dashboardReducer(undefined, { type: '@@INIT' }), unreadSlots: restoreUnreadBadges(restored), unreadSince: restored }
    const after = dashboardReducer(rebooted, remoteSlotRead({ slot: 'legacy-L', readTs: '2026-01-01T00:00:01Z' }))
    expect(after.unreadSlots).not.toContain('legacy-L')
    expect((JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)['legacy-L']).toBeUndefined()
  })

  it('badge and watermark persist in ONE record write: no torn state exists for a booting sibling', () => {
    // GPT F3 regression (span cf2f388263a4, head a30c8e312): the old design
    // wrote the watermark map and the badge list as two setItem calls; a
    // sibling booting between them saw a watermark with no badge, pruned it
    // as an orphan, and a trailing stale relay then cleared the unguarded
    // badge. The record makes that state unrepresentable: one setItem
    // carries badge presence and watermark in the same JSON value.
    localStorage.setItem('mc-unread-shared', JSON.stringify({}))
    localStorage.setItem('mc-unread-slots', JSON.stringify([]))
    const orig = Storage.prototype.setItem
    const writes: Array<{ key: string; value: string }> = []
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) {
      writes.push({ key, value })
      orig.call(this, key, value)
    })
    try {
      let st = dashboardReducer(undefined, { type: '@@INIT' })
      st = dashboardReducer(st, markSlotUnread({ slot: 'atomic-A', ts: '2026-01-01T00:00:07Z' }))
      expect(st.unreadSlots).toContain('atomic-A')
      const recordWrites = writes.filter(w => w.key === 'mc-unread-shared')
      expect(recordWrites).toHaveLength(1)  // ONE write carries both facts
      expect((JSON.parse(recordWrites[0].value) as Record<string, string>)['atomic-A']).toBe('2026-01-01T00:00:07Z')
    } finally {
      spy.mockRestore()
    }
    // A sibling booting at ANY point sees the pair together: restore yields
    // the badge WITH its watermark and prunes nothing.
    const restored = restoreUnreadSince()
    expect(restored['atomic-A']).toBe('2026-01-01T00:00:07Z')
    expect(restoreUnreadBadges(restored)).toContain('atomic-A')
    expect((JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}') as Record<string, string>)['atomic-A']).toBe('2026-01-01T00:00:07Z')
  })

  it('a superseded slot switch does not relay a read for a transcript that never rendered', async () => {
    // GPT F1 (head 92517e4b7): rapid A->B switch — A's fetch resolves after B
    // took over. The emit is gated on this request still owning activeSlot,
    // so the late resolve relays nothing.
    const { ws } = mount()
    const p1 = testStore.dispatch(switchSlot(BACKGROUND) as never) as unknown as Promise<unknown>
    const p2 = testStore.dispatch(switchSlot(ACTIVE) as never) as unknown as Promise<unknown>
    await act(async () => { await Promise.all([p1, p2]) })
    const frames = sentReadFrames(ws).map(f => JSON.parse(f) as { type: string; slot: string })
    // The superseded BACKGROUND request must not have relayed...
    expect(frames.some(f => f.slot === BACKGROUND)).toBe(false)
    // ...while the owning switch did.
    expect(frames.some(f => f.slot === ACTIVE)).toBe(true)
  })

  it('switchSlot relays the newest last_ts known AT emit time, not a stale pre-fetch capture', async () => {
    // GPT advisory (head 146de09ba): messages fetched during a reconnect
    // window arrive while the transcript loads; the relay must carry the
    // newest slot ts known when it EMITS, or sibling badges for those
    // messages survive a read that displayed them.
    const { ws } = mount()
    testStore.dispatch(addSlotOptimistic({ key: BACKGROUND, last_ts: '2026-01-01T00:00:01Z' } as never))
    const p = testStore.dispatch(switchSlot(BACKGROUND) as never) as unknown as Promise<unknown>
    // last_ts advances while the fetch is in flight (WS slots frame).
    testStore.dispatch(updateSlot({ key: BACKGROUND, last_ts: '2026-01-01T00:00:09Z' }))
    await act(async () => { await p })
    const frames = sentReadFrames(ws).map(f => JSON.parse(f) as { type: string; slot: string; read_ts?: string })
    expect(frames.some(f => f.slot === BACKGROUND && f.read_ts === '2026-01-01T00:00:09Z')).toBe(true)
    expect(frames.some(f => f.slot === BACKGROUND && f.read_ts === '2026-01-01T00:00:01Z')).toBe(false)
  })

  it('the hub relay carries this window\'s own unread count, manual reminders included', () => {
    // Opus advisory (head 146de09ba): the shared record omits sentinels, so
    // relaying ITS key count under-reports the hub chip. The reducers relay
    // state.unreadSlots instead, on every unread mutation.
    const posted: number[] = []
    const fakeParent = { postMessage: (msg: unknown) => { posted.push((msg as { count: number }).count) } }
    const spy = vi.spyOn(window, 'parent', 'get').mockReturnValue(fakeParent as unknown as Window)
    try {
      let st = dashboardReducer(undefined, { type: '@@INIT' })
      st = dashboardReducer(st, markSlotUnread('manual-H'))                                    // manual only
      expect(posted[posted.length - 1]).toBe(st.unreadSlots.length)                            // counted
      st = dashboardReducer(st, markSlotUnread({ slot: 'msg-H', ts: '2026-01-01T00:00:05Z' })) // + message
      expect(posted[posted.length - 1]).toBe(2)                                                // sentinel NOT dropped
      st = dashboardReducer(st, remoteSlotRead({ slot: 'msg-H', readTs: '2026-01-01T00:00:05Z' }))
      expect(posted[posted.length - 1]).toBe(1)                                                // manual survives the clear
      expect(st.unreadSlots).toEqual(['manual-H'])
    } finally {
      spy.mockRestore()
    }
  })

  it('reducer-level watermark rules: manual sentinel and newest-ts retention', () => {
    let st = dashboardReducer(undefined, { type: '@@INIT' })
    st = dashboardReducer(st, markSlotUnread('manual-slot'))                              // manual
    expect(st.unreadSince['manual-slot']).toBe(MANUAL_UNREAD)
    st = dashboardReducer(st, markSlotUnread({ slot: 'msg-slot', ts: '2026-01-01T00:00:09Z' }))
    st = dashboardReducer(st, markSlotUnread({ slot: 'msg-slot', ts: '2026-01-01T00:00:04Z' }))  // older arrival
    expect(st.unreadSince['msg-slot']).toBe('2026-01-01T00:00:09Z')                       // newest wins
    st = dashboardReducer(st, remoteSlotRead({ slot: 'manual-slot', readTs: '2099-01-01T00:00:00Z' }))
    expect(st.unreadSlots).toContain('manual-slot')                                       // shielded
    st = dashboardReducer(st, remoteSlotRead({ slot: 'msg-slot', readTs: '2026-01-01T00:00:08Z' }))
    expect(st.unreadSlots).toContain('msg-slot')                                          // older read keeps badge
    st = dashboardReducer(st, remoteSlotRead({ slot: 'msg-slot', readTs: '2026-01-01T00:00:09Z' }))
    expect(st.unreadSlots).not.toContain('msg-slot')                                      // covering read clears
    expect(st.unreadSince['msg-slot']).toBeUndefined()
  })
})

describe('hidden-tab reveal relay (single store end-to-end)', () => {
  // The hook dispatches through the Provider store and the relay reads the
  // singleton; in the app they are the same object. These specs mount the
  // Provider ON the singleton so the flush -> last_ts -> reveal-watermark
  // chain runs against one store, as it does in production.
  beforeEach(() => {
    vi.clearAllMocks()
    _resetSlotReadRelayForTest()
    WS_INSTANCES.length = 0
    vi.stubGlobal('WebSocket', MockWebSocket)
    globalStore.dispatch(addSlotOptimistic({ key: ACTIVE, title: ACTIVE, messages: 0, running: false } as ChatSlot))
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })

  afterEach(() => {
    _resetSlotReadRelayForTest()
    setDocumentHidden(false)
    vi.unstubAllGlobals()
    globalStore.dispatch(removeSlotOptimistic(ACTIVE))
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function singleStoreWrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: globalStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mountOnSingleton() {
    const hook = renderHook(() => useWebSocket(), { wrapper: singleStoreWrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    return { ...hook, ws }
  }

  const sentReadFrames = (ws: MockWebSocket) =>
    ws.send.mock.calls.map(c => c[0] as string).filter(f => f.includes('"slot_read"'))

  it('a hidden-tab arrival relays nothing; reveal relays the read at post-flush last_ts', () => {
    const { ws } = mountOnSingleton()
    act(() => { setDocumentHidden(true) })
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:00Z' } })
    })
    expect(sentReadFrames(ws)).toEqual([])         // hidden window isn't reading
    act(() => { setDocumentHidden(false) })        // …returning to it IS
    // Reveal flushes the buffered recency bump (rAF is parked in hidden tabs)
    // and relays the active slot at the flushed last_ts — the arrival's own ts.
    expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:00Z' })])
  })

  it('a visible-but-unfocused window relays nothing on arrival (occluded window is not reading)', () => {
    // UX finding (span 02afc2e81113): Page Visibility reports occluded or
    // merely unfocused windows as "visible", so without a focus gate a
    // window parked behind other apps would mark every arrival read within
    // ~1s — clearing badges on the window the user is actually using.
    const { ws } = mountOnSingleton()
    setDocumentFocused(false)                      // visible, NOT focused
    try {
      act(() => {
        ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:10Z' } })
      })
      expect(sentReadFrames(ws)).toEqual([])       // parked window isn't reading
      // Refocusing the window (a focus-only change fires NO visibilitychange)
      // re-announces the slot and relays the read at the slot's last_ts —
      // without it the suppressed relay would never fire and sibling badges
      // would stay stale until the next gesture (GPT F3, head 4f1aac79f).
      setDocumentFocused(true)
      act(() => { window.dispatchEvent(new Event('focus')) })
      expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:10Z' })])
    } finally {
      setDocumentFocused(true)
    }
    // (A direct focused-arrival relay is covered by the arrival tests above;
    // an immediate follow-up here would only exercise the per-slot throttle's
    // trailing coalescing.)
  })

  it('a timestamp-less chat_done while hidden cannot regress the reveal watermark', () => {
    const { ws } = mountOnSingleton()
    act(() => { setDocumentHidden(true) })
    act(() => {
      ws.simulateMessage({ type: 'chat_message', data: { slot: ACTIVE, role: 'assistant', content: 'hi', ts: '2026-09-10T00:00:05Z' } })
    })
    act(() => { ws.simulateMessage({ type: 'chat_done', data: { slot: ACTIVE } }) })   // no ts on the frame
    act(() => { setDocumentHidden(false) })
    // No per-arrival state exists for the ts-less chat_done to overwrite: the
    // reveal reads the slot's own post-flush last_ts, which the reducer keeps
    // monotonic, so the relay carries the newest arrival's ts.
    expect(sentReadFrames(ws)).toEqual([JSON.stringify({ type: 'slot_read', slot: ACTIVE, read_ts: '2026-09-10T00:00:05Z' })])
  })
})
