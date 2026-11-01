import { describe, it, expect, vi } from 'vitest'
import reducer, {
  sseStatus,
  setYoloDuration,
  sseConnected,
  sseDisconnected,
  sseSlots,
  sseSlotsPartial,
  sseSlotListHolds,
  touchSlotActivity,
  sseSlotTitle,
  addSlotOptimistic,
  removeSlotOptimistic,
  triggerRefresh,
  markSlotUnread,
  markSlotRead,
  remoteSlotRead,
  fetchSlots,
  selectUnreadByMode,
  patchSlotLink,
  dropSlotLinks,
} from '../store/dashboardSlice'
import type { StatusData, ChatSlot } from '../types'
import { sharedItem } from './unreadStorageTestUtils'
import { SHARED_UNREAD_FLUSH_MS, flushSharedUnread, relayedUnreadKeys } from '../store/dashboard/unread'
import { crewModePreviewOn } from './crewModePreview'

vi.mock('../api/client', () => ({
  api: { chatSlots: vi.fn(), chatMode: vi.fn() },
}))

const slot1: ChatSlot = { key: 'chat-1', title: 'Chat 1', messages: 5, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }
const slot2: ChatSlot = { key: 'chat-2', title: 'Chat 2', messages: 3, running: true, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }

describe('dashboardSlice', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('has correct initial state', () => {
    expect(initial.status).toBeNull()
    expect(initial.connected).toBe(false)
    expect(initial.slots).toEqual([])
    expect(initial.approvalMode).toBe('normal')
    expect(initial.refreshTrigger).toBe(0)
    expect(initial.unreadSlots).toEqual([])
  })

  describe('sseStatus', () => {
    it('sets status and connected', () => {
      const status = { uptime: '1h', sessions: 2, messages: 10, cron_jobs: 0, subagents: 0, lessons: 0 } as StatusData
      const state = reducer(initial, sseStatus(status))
      expect(state.status).toEqual(status)
      expect(state.connected).toBe(true)
    })

    it('syncs yolo mode from backend', () => {
      const status = { uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, yolo: true } as StatusData
      const state = reducer(initial, sseStatus(status))
      expect(state.approvalMode).toBe('yolo')
    })

    it('reverts from yolo when backend says false', () => {
      const yoloState = { ...initial, approvalMode: 'yolo' }
      const status = { uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, yolo: false } as StatusData
      const state = reducer(yoloState, sseStatus(status))
      expect(state.approvalMode).toBe('normal')
    })

    it('carries the config-derived grant keys across a WebSocket frame that omits them', () => {
      const http = {
        uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
        yolo_duration: '1h', yolo_until_shutdown_permitted: false,
      } as StatusData
      const wsFrame = { uptime: '2h', sessions: 3, messages: 5, cron_jobs: 1, subagents: 0, lessons: 2 } as StatusData
      const state = reducer(reducer(initial, sseStatus(http)), sseStatus(wsFrame))
      expect(state.status).toEqual({ ...wsFrame, yolo_duration: '1h', yolo_until_shutdown_permitted: false })
    })

    it('still replaces every other key a frame omits (an omitted key is an answer)', () => {
      const http = {
        uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
        version_display: '0.4.0', yolo_expires_at: '2026-01-01T00:00:00Z', yolo_until_shutdown: true,
      } as StatusData
      const wsFrame = { uptime: '2h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 } as StatusData
      const state = reducer(reducer(initial, sseStatus(http)), sseStatus(wsFrame))
      expect(state.status).toEqual(wsFrame)
    })

    it('lets a frame that carries a grant key overwrite the retained value', () => {
      const first = { uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, yolo_duration: '1h' } as StatusData
      const second = { ...first, yolo_duration: '24h' } as StatusData
      const state = reducer(reducer(initial, sseStatus(first)), sseStatus(second))
      expect(state.status?.yolo_duration).toBe('24h')
    })

    it('setYoloDuration writes the saved value and it outranks later frames and replies', () => {
      const http = { uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0, yolo_duration: '30m' } as StatusData
      const wsFrame = { uptime: '2h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 } as StatusData
      let state = reducer(initial, sseStatus(http))
      state = reducer(state, setYoloDuration('24h'))
      expect(state.status?.yolo_duration).toBe('24h')
      // A frame without the key carries the save; a stale reply WITH the old
      // key (a request that began before the save) does not roll it back.
      state = reducer(state, sseStatus(wsFrame))
      expect(state.status?.yolo_duration).toBe('24h')
      state = reducer(state, sseStatus(http))
      expect(state.status?.yolo_duration).toBe('24h')
    })

    it('a save recorded before any status arrives is applied to the first status', () => {
      const wsFrame = { uptime: '2h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 } as StatusData
      let state = reducer(initial, setYoloDuration('1h'))
      expect(state.status).toBeNull()
      state = reducer(state, sseStatus(wsFrame))
      expect(state.status?.yolo_duration).toBe('1h')
    })
  })

  it('sseConnected sets connected true', () => {
    expect(reducer(initial, sseConnected()).connected).toBe(true)
  })

  it('sseDisconnected sets connected false', () => {
    const connected = { ...initial, connected: true }
    expect(reducer(connected, sseDisconnected()).connected).toBe(false)
  })

  it('sseSlots replaces slots', () => {
    const state = reducer(initial, sseSlots([slot1, slot2]))
    expect(state.slots).toHaveLength(2)
  })

  it('sseSlotTitle updates matching slot title', () => {
    const withSlots = reducer(initial, sseSlots([slot1, slot2]))
    const state = reducer(withSlots, sseSlotTitle({ key: 'chat-1', title: 'Renamed' }))
    expect(state.slots[0].title).toBe('Renamed')
    expect(state.slots[1].title).toBe('Chat 2')
  })

  describe('touchSlotActivity', () => {
    it('bumps the matching slot last_ts to the supplied timestamp', () => {
      const withSlots = reducer(initial, sseSlots([slot1, slot2]))
      const state = reducer(withSlots, touchSlotActivity({ key: 'chat-1', ts: '2026-07-09T22:00:00Z' }))
      expect(state.slots.find(s => s.key === 'chat-1')?.last_ts).toBe('2026-07-09T22:00:00Z')
      expect(state.slots.find(s => s.key === 'chat-2')?.last_ts).toBeUndefined()
    })

    it('leaves the ORDERING key alone for un-settled activity', () => {
      // Agent output moves last_ts but must not re-rank the sidebar: a session
      // streaming tool calls would otherwise climb over its neighbours on every
      // event, swapping rows under the pointer while several agents work.
      const withSlots = reducer(initial, sseSlots([slot1]))
      const state = reducer(withSlots, touchSlotActivity({ key: 'chat-1', ts: '2026-07-09T22:00:00Z' }))
      expect(state.slots[0].last_turn_ts).toBeUndefined()
    })

    it('bumps last_turn_ts too when the activity is settled', () => {
      // An inbound prompt SHOULD move the session to the top immediately — the
      // user just acted on it.
      const withSlots = reducer(initial, sseSlots([slot1]))
      const state = reducer(withSlots, touchSlotActivity({ key: 'chat-1', ts: '2026-07-09T22:00:00Z', settled: true }))
      expect(state.slots[0].last_turn_ts).toBe('2026-07-09T22:00:00Z')
      expect(state.slots[0].last_ts).toBe('2026-07-09T22:00:00Z')
    })

    it('is a no-op for an unknown slot key', () => {
      const withSlots = reducer(initial, sseSlots([slot1]))
      const state = reducer(withSlots, touchSlotActivity({ key: 'missing', ts: '2026-07-09T22:00:00Z' }))
      expect(state.slots).toHaveLength(1)
      expect(state.slots[0].last_ts).toBeUndefined()
    })

    it('never moves either field backwards', () => {
      // An authoritative slots snapshot can land between an event being buffered
      // and dispatched; an older arrival time must not undo it.
      const withSlots = reducer(initial, sseSlots([
        { ...slot1, last_ts: '2026-07-09T22:00:00Z', last_turn_ts: '2026-07-09T21:00:00Z' },
      ]))
      const state = reducer(withSlots, touchSlotActivity({ key: 'chat-1', ts: '2026-07-09T20:00:00Z', settled: true }))
      expect(state.slots[0].last_ts).toBe('2026-07-09T22:00:00Z')
      expect(state.slots[0].last_turn_ts).toBe('2026-07-09T21:00:00Z')
    })

    it('applies a settling bump that is older than last_ts but newer than last_turn_ts', () => {
      // Mid-turn the two fields diverge: last_ts is a streamed tool row, so a
      // prompt arriving behind it is still the newest SETTLED instant. A shared
      // monotonic check would silently drop it.
      const withSlots = reducer(initial, sseSlots([
        { ...slot1, last_ts: '2026-07-09T22:00:00Z', last_turn_ts: '2026-07-09T20:00:00Z' },
      ]))
      const state = reducer(withSlots, touchSlotActivity({ key: 'chat-1', ts: '2026-07-09T21:00:00Z', settled: true }))
      expect(state.slots[0].last_ts).toBe('2026-07-09T22:00:00Z')
      expect(state.slots[0].last_turn_ts).toBe('2026-07-09T21:00:00Z')
    })
  })

  it('addSlotOptimistic adds if not present', () => {
    const state = reducer(initial, addSlotOptimistic(slot1))
    expect(state.slots).toHaveLength(1)
    // Adding same key again should not duplicate
    const state2 = reducer(state, addSlotOptimistic(slot1))
    expect(state2.slots).toHaveLength(1)
  })

  it('removeSlotOptimistic removes by key', () => {
    const withSlots = reducer(initial, sseSlots([slot1, slot2]))
    const state = reducer(withSlots, removeSlotOptimistic('chat-1'))
    expect(state.slots).toHaveLength(1)
    expect(state.slots[0].key).toBe('chat-2')
  })

  it('removeSlotOptimistic also clears unread for removed slot', () => {
    let state = reducer(initial, sseSlots([slot1, slot2]))
    state = reducer(state, markSlotUnread('chat-1'))
    state = reducer(state, removeSlotOptimistic('chat-1'))
    expect(state.unreadSlots).toEqual([])
  })

  it('fetchSlots.fulfilled reconciles unreadSlots against live slots', () => {
    // A complete slots frame has released the boot-time unread hold.
    let state = reducer(initial, sseSlotsPartial(false))
    state = reducer(state, sseSlots([slot1, slot2]))
    state = reducer(state, markSlotUnread('chat-1'))
    state = reducer(state, markSlotUnread('chat-2'))
    // Simulate fetchSlots returning only slot2 (slot1 was deleted remotely)
    state = reducer(state, fetchSlots.fulfilled([slot2], 'requestId'))
    expect(state.unreadSlots).toEqual(['chat-2'])
  })

  describe('unread drain hold (partial slot list)', () => {
    // A gateway mid-restart labels its slots frames `slotsPartial` while
    // sessions are still being restored. A session not yet restored is absent
    // from such a list without having been deleted, so the drain that clears a
    // remotely deleted slot's badge must wait for a complete list.
    it('holds the drain from boot until a complete frame arrives', () => {
      expect(initial.unreadDrainHeld).toBe(true)
      const released = reducer(initial, sseSlotsPartial(false))
      expect(released.unreadDrainHeld).toBe(false)
      expect(reducer(released, sseSlotsPartial(true)).unreadDrainHeld).toBe(true)
    })

    it('re-arms the hold on every connect', () => {
      const released = reducer(initial, sseSlotsPartial(false))
      expect(reducer(released, sseConnected()).unreadDrainHeld).toBe(true)
    })

    it('a complete list drains other unread keys but preserves held keys', () => {
      const slot3 = { ...slot1, key: 'chat-3', title: 'Chat 3' }
      let state = reducer(initial, sseSlotsPartial(false))
      state = reducer(state, sseSlots([slot1, slot2, slot3]))
      state = reducer(state, markSlotUnread('chat-1'))
      state = reducer(state, markSlotUnread('chat-3'))
      state = reducer(state, sseSlotListHolds({ heldKeys: ['chat-1'], holdChannels: false }))

      state = reducer(state, sseSlots([slot2]))

      expect(state.unreadSlots).toEqual(['chat-1'])
    })

    it('a channel-wide hold preserves channel unread keys while draining ordinary keys', () => {
      const channel = { ...slot1, key: 'slack_1.1', title: 'Slack thread' }
      let state = reducer(initial, sseSlotsPartial(false))
      state = reducer(state, sseSlots([channel, slot1]))
      state = reducer(state, markSlotUnread('slack_1.1'))
      state = reducer(state, markSlotUnread('chat-1'))
      state = reducer(state, sseSlotListHolds({ heldKeys: [], holdChannels: true }))

      state = reducer(state, sseSlots([]))

      expect(state.unreadSlots).toEqual(['slack_1.1'])
    })

    it('an embedded instance relays an unread count without held orphans', () => {
      const posted: Array<{ type?: string; count?: number }> = []
      const realParent = Object.getOwnPropertyDescriptor(window, 'parent')
      Object.defineProperty(window, 'parent', {
        value: { postMessage: (m: { type?: string; count?: number }) => { posted.push(m) } },
        configurable: true,
      })
      try {
        const channel = { ...slot1, key: 'slack_1.1', title: 'Slack thread' }
        const lastCount = () => posted.filter(m => m.type === 'mc-unread-slots').at(-1)?.count
        let state = reducer(initial, sseSlotsPartial(false))
        state = reducer(state, sseSlots([slot1, slot2, channel]))
        state = reducer(state, markSlotUnread('chat-1'))
        state = reducer(state, markSlotUnread('chat-2'))
        state = reducer(state, markSlotUnread('slack_1.1'))
        expect(lastCount()).toBe(3)

        // chat-1 and the channel thread go absent but are held: kept, not counted.
        state = reducer(state, sseSlotListHolds({ heldKeys: ['chat-1'], holdChannels: true }))
        state = reducer(state, sseSlots([slot2]))
        expect(state.unreadSlots).toEqual(expect.arrayContaining(['chat-1', 'slack_1.1']))
        expect(lastCount()).toBe(1)

        // chat-1 is listed again, so it counts again.
        state = reducer(state, sseSlots([slot1, slot2]))
        expect(lastCount()).toBe(2)
      } finally {
        if (realParent) Object.defineProperty(window, 'parent', realParent)
      }
    })

    it('an embedded instance recounts held unread sessions when reconnect clears the holds', () => {
      const posted: Array<{ type?: string; count?: number }> = []
      const realParent = Object.getOwnPropertyDescriptor(window, 'parent')
      Object.defineProperty(window, 'parent', {
        value: { postMessage: (m: { type?: string; count?: number }) => { posted.push(m) } },
        configurable: true,
      })
      try {
        const lastCount = () => posted.filter(m => m.type === 'mc-unread-slots').at(-1)?.count
        let state = reducer(initial, sseSlotsPartial(false))
        state = reducer(state, sseSlots([slot1, slot2]))
        state = reducer(state, markSlotUnread('chat-1'))
        state = reducer(state, markSlotUnread('chat-2'))
        state = reducer(state, sseSlotListHolds({ heldKeys: ['chat-1'], holdChannels: false }))
        state = reducer(state, sseSlots([slot2]))
        expect(lastCount()).toBe(1)

        state = reducer(state, sseConnected())

        expect(state.slotsHeldKeys).toEqual([])
        expect(lastCount()).toBe(2)
      } finally {
        if (realParent) Object.defineProperty(window, 'parent', realParent)
      }
    })

    it('an embedded instance recounts a held unread session on an optimistic add', () => {
      const posted: Array<{ type?: string; count?: number }> = []
      const realParent = Object.getOwnPropertyDescriptor(window, 'parent')
      Object.defineProperty(window, 'parent', {
        value: { postMessage: (m: { type?: string; count?: number }) => { posted.push(m) } },
        configurable: true,
      })
      try {
        const lastCount = () => posted.filter(m => m.type === 'mc-unread-slots').at(-1)?.count
        let state = reducer(initial, sseSlotsPartial(false))
        state = reducer(state, sseSlots([slot1, slot2]))
        state = reducer(state, markSlotUnread('chat-1'))
        state = reducer(state, markSlotUnread('chat-2'))
        state = reducer(state, sseSlotListHolds({ heldKeys: ['chat-1'], holdChannels: false }))
        state = reducer(state, sseSlots([slot2]))
        expect(lastCount()).toBe(1)

        reducer(state, addSlotOptimistic(slot1))

        expect(lastCount()).toBe(2)
      } finally {
        if (realParent) Object.defineProperty(window, 'parent', realParent)
      }
    })

    it('an embedded instance recounts a held unread session on a fulfilled create', () => {
      const posted: Array<{ type?: string; count?: number }> = []
      const realParent = Object.getOwnPropertyDescriptor(window, 'parent')
      Object.defineProperty(window, 'parent', {
        value: { postMessage: (m: { type?: string; count?: number }) => { posted.push(m) } },
        configurable: true,
      })
      try {
        const lastCount = () => posted.filter(m => m.type === 'mc-unread-slots').at(-1)?.count
        let state = reducer(initial, sseSlotsPartial(false))
        state = reducer(state, sseSlots([slot1, slot2]))
        state = reducer(state, markSlotUnread('chat-1'))
        state = reducer(state, markSlotUnread('chat-2'))
        state = reducer(state, sseSlotListHolds({ heldKeys: ['chat-1'], holdChannels: false }))
        state = reducer(state, sseSlots([slot2]))
        expect(lastCount()).toBe(1)

        reducer(state, { type: 'chat/createSlot/fulfilled', payload: slot1 })

        expect(lastCount()).toBe(2)
      } finally {
        if (realParent) Object.defineProperty(window, 'parent', realParent)
      }
    })

    it('an embedded instance recounts a tab reopened under the partial label with nothing held', () => {
      const posted: Array<{ type?: string; count?: number }> = []
      const realParent = Object.getOwnPropertyDescriptor(window, 'parent')
      Object.defineProperty(window, 'parent', {
        value: { postMessage: (m: { type?: string; count?: number }) => { posted.push(m) } },
        configurable: true,
      })
      try {
        const lastCount = () => posted.filter(m => m.type === 'mc-unread-slots').at(-1)?.count
        let state = reducer(initial, sseSlotsPartial(false))
        state = reducer(state, sseSlots([slot1, slot2]))
        state = reducer(state, markSlotUnread('chat-1'))
        state = reducer(state, markSlotUnread('chat-2'))
        expect(lastCount()).toBe(2)

        // An unreadable tab registry: the list is partial and names no held keys.
        state = reducer(state, sseSlotsPartial(true))
        state = reducer(state, sseSlots([slot2]))
        expect(lastCount()).toBe(1)

        // chat-1 reopens while the label is still on: it counts again at once.
        reducer(state, sseSlots([slot1, slot2]))
        expect(lastCount()).toBe(2)
      } finally {
        if (realParent) Object.defineProperty(window, 'parent', realParent)
      }
    })

    it('an identical holds frame leaves the dashboard state untouched', () => {
      let state = reducer(initial, sseSlotListHolds({ heldKeys: ['chat-1'], holdChannels: true }))
      const again = reducer(state, sseSlotListHolds({ heldKeys: ['chat-1'], holdChannels: true }))
      expect(again).toBe(state)
      state = reducer(state, sseSlotListHolds({ heldKeys: ['chat-1', 'chat-2'], holdChannels: true }))
      expect(state.slotsHeldKeys).toEqual(['chat-1', 'chat-2'])
    })

    it('an HTTP list cannot clear hold state delivered by the WebSocket', () => {
      const slot3 = { ...slot1, key: 'chat-3', title: 'Chat 3' }
      let state = reducer(initial, sseSlotsPartial(false))
      state = reducer(state, sseSlots([slot1, slot2, slot3]))
      state = reducer(state, markSlotUnread('chat-1'))
      state = reducer(state, markSlotUnread('chat-3'))
      state = reducer(state, sseSlotListHolds({ heldKeys: ['chat-3'], holdChannels: false }))

      state = reducer(state, fetchSlots.fulfilled([slot2], 'held-keys-response'))

      expect(state.unreadSlots).toEqual(['chat-3'])
    })

    it('a GET started after disconnect cannot drain an absent unread key', () => {
      let state = reducer(initial, sseSlotsPartial(false))
      state = reducer(state, sseSlots([slot1, slot2]))
      state = reducer(state, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:00Z' }))
      expect(state.unreadDrainHeld).toBe(false)

      state = reducer(state, sseDisconnected())
      state = reducer(state, fetchSlots.pending('disconnected-request', undefined))
      state = reducer(state, fetchSlots.fulfilled([slot2], 'disconnected-request', undefined))

      expect(state.unreadSlots).toEqual(['chat-1'])
      expect(state.unreadSince['chat-1']).toBe('2026-01-01T00:00:00Z')
    })

    it('a GET reply during the hold does not drain an absent key', () => {
      // The reply was requested on connect and can land before the connect
      // snapshot says whether the list is complete; the hold set by
      // `sseConnected` covers it.
      let state = reducer(initial, sseSlotsPartial(false))
      state = reducer(state, sseSlots([slot1, slot2]))
      state = reducer(state, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:00Z' }))
      state = reducer(state, sseConnected())
      state = reducer(state, fetchSlots.fulfilled([slot2], 'requestId'))
      expect(state.slots.map(s => s.key)).toEqual(['chat-2'])
      expect(state.unreadSlots).toEqual(['chat-1'])
      expect(state.unreadSince['chat-1']).toBe('2026-01-01T00:00:00Z')
      expect(Object.keys(JSON.parse(sharedItem('mc-unread-shared') ?? '{}'))).toContain('chat-1')
      // The first complete frame drains what is genuinely gone.
      state = reducer(state, sseSlotsPartial(false))
      state = reducer(state, sseSlots([slot2]))
      expect(state.unreadSlots).toEqual([])
      expect(state.unreadSince['chat-1']).toBeUndefined()
      expect(Object.keys(JSON.parse(sharedItem('mc-unread-shared') ?? '{}'))).not.toContain('chat-1')
    })

    it('a GET started during the hold cannot drain after a complete frame releases it', () => {
      localStorage.clear()
      let state = reducer(initial, sseSlotsPartial(false))
      state = reducer(state, sseSlots([slot1, slot2]))
      state = reducer(state, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:00Z' }))
      state = reducer(state, sseConnected())
      state = reducer(state, fetchSlots.pending('held-request', undefined))
      state = reducer(state, sseSlotsPartial(false))
      state = reducer(state, sseSlots([slot1, slot2]))
      expect(state.unreadDrainHeld).toBe(false)

      state = reducer(state, fetchSlots.fulfilled([slot2], 'held-request', undefined))

      expect(state.unreadSlots).toEqual(['chat-1'])
      expect(state.unreadSince['chat-1']).toBe('2026-01-01T00:00:00Z')
      expect(JSON.parse(sharedItem('mc-unread-shared') ?? '{}')).toMatchObject({
        'chat-1': '2026-01-01T00:00:00Z',
      })
      expect(state.slotFetchWriteMark['held-request']).toBeUndefined()
    })

    it('a GET started after the hold clears still drains an absent key', () => {
      localStorage.clear()
      let state = reducer(initial, sseSlotsPartial(false))
      state = reducer(state, sseSlots([slot1, slot2]))
      state = reducer(state, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:00Z' }))
      state = reducer(state, fetchSlots.pending('unheld-request', undefined))

      state = reducer(state, fetchSlots.fulfilled([slot2], 'unheld-request', undefined))

      expect(state.unreadSlots).toEqual([])
      expect(state.unreadSince['chat-1']).toBeUndefined()
      expect(JSON.parse(sharedItem('mc-unread-shared') ?? '{}')).not.toHaveProperty('chat-1')
      expect(state.slotFetchWriteMark['unheld-request']).toBeUndefined()
    })
  })

  it('triggerRefresh increments counter', () => {
    const state = reducer(initial, triggerRefresh())
    expect(state.refreshTrigger).toBe(1)
    const state2 = reducer(state, triggerRefresh())
    expect(state2.refreshTrigger).toBe(2)
  })

  describe('unread slots', () => {
    it('markSlotUnread adds slot key', () => {
      const state = reducer(initial, markSlotUnread('chat-1'))
      expect(state.unreadSlots).toEqual(['chat-1'])
    })

    it('markSlotUnread does not duplicate', () => {
      let state = reducer(initial, markSlotUnread('chat-1'))
      state = reducer(state, markSlotUnread('chat-1'))
      expect(state.unreadSlots).toEqual(['chat-1'])
    })

    it('markSlotRead removes slot key', () => {
      let state = reducer(initial, markSlotUnread('chat-1'))
      state = reducer(state, markSlotUnread('chat-2'))
      state = reducer(state, markSlotRead('chat-1'))
      expect(state.unreadSlots).toEqual(['chat-2'])
    })

    it('markSlotRead is a no-op for unknown key', () => {
      const state = reducer(initial, markSlotRead('nonexistent'))
      expect(state.unreadSlots).toEqual([])
    })

    // The shared unread record is written by an ordinary arrival, and that write
    // is on the websocket `onmessage` -> Redux dispatch -> re-render path. A
    // QuotaExceededError raised there is swallowed by the surrounding try/catch,
    // so the record is silently lost while megabytes of re-derivable cache sit
    // next to it. `safeSetItem` reclaims a disposable tier and retries; the raw
    // write does not. This pins the reclaim so the record survives a full quota.
    it('markSlotUnread reclaims disposable cache and still persists when the quota is full', () => {
      const quota = () => {
        const e = new DOMException('quota', 'QuotaExceededError')
        Object.defineProperty(e, 'code', { value: 22, configurable: true })
        return e
      }
      // Disposable cache the reclaim tiers are allowed to drop.
      localStorage.setItem('vc_heights_session-A', '{"a":1}')
      localStorage.setItem('keep-me', 'important')

      const real = Storage.prototype.setItem
      const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
        this: Storage,
        key: string,
        value: string,
      ) {
        // Fail only while the reclaimable cache is still present, so a retry
        // after reclaim succeeds and a non-reclaiming writer never does.
        if (this.getItem('vc_heights_session-A') !== null && !key.startsWith('vc_heights_')) {
          throw quota()
        }
        real.call(this, key, value)
      })

      try {
        reducer(initial, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:00Z' }))
        flushSharedUnread()
      } finally {
        spy.mockRestore()
      }

      // The shared record survived because the write reclaimed space first.
      expect(JSON.parse(sharedItem('mc-unread-shared') ?? '{}')).toEqual({
        'chat-1': '2026-01-01T00:00:00Z',
      })
      // Disposable cache was the thing sacrificed, not the record.
      expect(localStorage.getItem('vc_heights_session-A')).toBeNull()
      expect(localStorage.getItem('keep-me')).toBe('important')
    })

    // `mc-unread-slots` is a PROJECTION of the shared record's keys, so the two
    // must not disagree. The projection is strictly smaller than the record
    // (keys only, no timestamps), so writing it can free space and succeed on a
    // quota where the record's own write just failed. The old code could not
    // reach that state: the raw setItem THREW, the surrounding catch swallowed
    // it, and the projection line never ran. A helper that reports failure by
    // return value instead of throwing silently removed that protection, which
    // is the whole risk of converting a throwing call in a try/catch body.
    it('leaves the projection alone when the shared record write fails', () => {
      const quota = () => {
        const e = new DOMException('quota', 'QuotaExceededError')
        Object.defineProperty(e, 'code', { value: 22, configurable: true })
        return e
      }
      // This file has no storage-clearing beforeEach, so start from a known
      // state rather than whatever the previous case left behind.
      localStorage.clear()
      // A stale, larger projection than the one this dispatch would write, so a
      // projection write would shrink it and find room.
      localStorage.setItem('mc-unread-slots', JSON.stringify(['chat-1', 'chat-2', 'chat-3']))

      const real = Storage.prototype.setItem
      const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
        this: Storage,
        key: string,
        value: string,
      ) {
        // The authoritative record cannot be written; the projection still can.
        if (key === 'mc-unread-shared') throw quota()
        real.call(this, key, value)
      })

      try {
        reducer(initial, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:00Z' }))
        flushSharedUnread()
      } finally {
        spy.mockRestore()
      }

      // The record did not land, so the projection must not have been advanced
      // past it. Two persisted records that disagree is worse than neither
      // being written: `restoreUnreadSince` trusts the record while older tabs
      // and the hub relay read the projection.
      expect(sharedItem('mc-unread-shared')).toBeNull()
      expect(JSON.parse(sharedItem('mc-unread-slots') ?? '[]')).toEqual([
        'chat-1',
        'chat-2',
        'chat-3',
      ])
    })

    // A background slot re-marks itself on every arriving row. The reducer
    // queues the shared write instead of doing it, and one flush writes every
    // queued arrival in a single record write.
    it('queues arrivals and writes them in one record write', () => {
      localStorage.clear()
      const spy = vi.spyOn(Storage.prototype, 'setItem')
      vi.useFakeTimers()
      try {
        let s = reducer(initial, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:05Z' }))
        s = reducer(s, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:09Z' }))
        s = reducer(s, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:01Z' }))
        s = reducer(s, markSlotUnread({ slot: 'chat-2' }))
        expect(spy.mock.calls.filter(([k]) => k.startsWith('mc-unread-'))).toEqual([])
        vi.advanceTimersByTime(SHARED_UNREAD_FLUSH_MS)
        expect(spy.mock.calls.map(([k]) => k)).toEqual(['mc-unread-shared', 'mc-unread-slots'])
        expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual({
          'chat-1': '2026-01-01T00:00:09Z',
          'chat-2': '',
        })
        expect(s.unreadSlots).toEqual(['chat-1', 'chat-2'])
      } finally {
        spy.mockRestore()
        vi.useRealTimers()
      }
    })

    // When the live record already holds the badge at that watermark, the
    // flush must not rewrite the record or its projection.
    it('skips the storage write when the live record already covers the arrival', () => {
      localStorage.clear()
      let s = reducer(initial, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:05Z' }))
      flushSharedUnread()
      const spy = vi.spyOn(Storage.prototype, 'setItem')
      try {
        s = reducer(s, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:05Z' }))
        s = reducer(s, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:01Z' }))
        s = reducer(s, markSlotUnread({ slot: 'chat-1' }))
        flushSharedUnread()
        expect(spy.mock.calls.filter(([k]) => k.startsWith('mc-unread-'))).toEqual([])
      } finally {
        spy.mockRestore()
      }
      expect(s.unreadSlots).toEqual(['chat-1'])
    })

    // A sibling window that read the message cleared the shared record before
    // this window's queued arrival was written. The relayed read must drop the
    // queued arrival it covers, even when this window keeps its own badge (a
    // manual reminder here), and keep a queued arrival newer than the read.
    it('a relayed read drops the queued arrival it covers', () => {
      localStorage.clear()
      let s = reducer(initial, markSlotUnread('chat-1'))
      s = reducer(s, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:05Z' }))
      s = reducer(s, markSlotUnread({ slot: 'chat-2', ts: '2026-01-01T00:00:09Z' }))
      s = reducer(s, remoteSlotRead({ slot: 'chat-1', readTs: '2026-01-01T00:00:05Z' }))
      s = reducer(s, remoteSlotRead({ slot: 'chat-2', readTs: '2026-01-01T00:00:05Z' }))
      flushSharedUnread()
      expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual({ 'chat-2': '2026-01-01T00:00:09Z' })
      // The manual reminder answers only to a local read, so its badge stays.
      expect(s.unreadSlots).toEqual(['chat-1', 'chat-2'])
    })

    // A read in this window right after an arrival must judge the record with
    // that arrival in it, or the queued write would land after the clear and
    // bring the badge back for sibling windows.
    it('writes queued arrivals before a local read clears the record', () => {
      localStorage.clear()
      let s = reducer(initial, sseSlots([{ key: 'chat-1', last_ts: '2026-01-01T00:00:09Z' } as never]))
      s = reducer(s, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:09Z' }))
      s = reducer(s, markSlotRead('chat-1'))
      flushSharedUnread()
      expect(JSON.parse(localStorage.getItem('mc-unread-shared') ?? '{}')).toEqual({})
      expect(s.unreadSlots).toEqual([])
    })

    // The skip reads the live record, so a sibling window that cleared the
    // shared entry still gets it back on this window's next arrival, even one
    // at the same watermark.
    it('re-publishes an arrival after a sibling cleared the shared entry', () => {
      localStorage.clear()
      let s = reducer(initial, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:05Z' }))
      localStorage.setItem('mc-unread-shared', '{}')
      localStorage.setItem('mc-unread-slots', '[]')
      s = reducer(s, markSlotUnread({ slot: 'chat-1', ts: '2026-01-01T00:00:05Z' }))
      expect(JSON.parse(sharedItem('mc-unread-shared') ?? '{}')).toEqual({ 'chat-1': '2026-01-01T00:00:05Z' })
      expect(JSON.parse(sharedItem('mc-unread-slots') ?? '[]')).toEqual(['chat-1'])
      expect(s.unreadSlots).toEqual(['chat-1'])
    })
  })

  describe('selectUnreadByMode', () => {
    // The bigger surface-level coverage (cross-surface-leaks-into-Chat
    // regression, orphan-key fallback, appOnly visibility) lives in
    // src/test/surfaces.test.tsx where the registry under test is.
    // Here we pin the underlying factory's contract: surface-key resolution
    // (slot.surface ?? slot.mode) and per-mode memoization.
    const buildState = (slots: ChatSlot[], unread: string[]) =>
      ({ dashboard: { ...initial, slots, unreadSlots: unread } } as unknown as Parameters<ReturnType<typeof selectUnreadByMode>>[0])

    it('honors slot.surface over slot.mode when both are present', () => {
      // Forward-compat: backend now emits an explicit `surface` field that
      // mirrors `mode` today but is allowed to diverge later. A slot whose
      // `mode === ''` but `surface === 'dashboard'` belongs to the dashboard
      // surface, not the chat badge.
      const slot: ChatSlot = { key: 'dash-1', title: 'D', messages: 0, running: false, mode: '', surface: 'dashboard' }
      const state = buildState([slot], ['dash-1'])
      expect(selectUnreadByMode('')(state)).toBe(0)
      expect(selectUnreadByMode('dashboard')(state)).toBe(1)
    })

    it('falls back to slot.mode when slot.surface is absent (back-compat)', () => {
      // Older backend payloads without a `surface` field must still route
      // via `mode` so a `surface`-aware client doesn't require a coupled deploy.
      const slot: ChatSlot = { key: 'dash-1', title: 'D', messages: 0, running: false, mode: 'dashboard' }
      const state = buildState([slot], ['dash-1'])
      expect(selectUnreadByMode('')(state)).toBe(0)
      expect(selectUnreadByMode('dashboard')(state)).toBe(1)
    })

    it('counts a legacy Autopilot slot toward the chat badge', () => {
      // A slot still persisted under the retired 'orchestrator' mode renders
      // as an ordinary chat, so its unread belongs to the chat surface ('').
      const bySurface: ChatSlot = { key: 'legacy-1', title: 'L', messages: 0, running: false, mode: '', surface: 'orchestrator' }
      const byMode: ChatSlot = { key: 'legacy-2', title: 'L', messages: 0, running: false, mode: 'orchestrator' }
      const state = buildState([bySurface, byMode], ['legacy-1', 'legacy-2'])
      expect(selectUnreadByMode('')(state)).toBe(2)
    })

    it('leaves a Crew Mode crew member out of the badge and the relayed count, keeping its unread entry', () => {
      // A crew member is no row of the person's lists (its root's Crew panel
      // lists it), so its finished turn must not count an unread session the
      // Sessions list never shows. The entry stays for the Crew panel.
      const root: ChatSlot = { key: 'root', title: 'R', messages: 1, running: false, mode: '', crew_mode_root: true }
      const member: ChatSlot = { key: 'w1', title: 'W', messages: 1, running: false, mode: '', crew_root: 'root' }
      const plain: ChatSlot = { key: 'chat-1', title: 'C', messages: 1, running: false, mode: '' }
      const state = buildState([root, member, plain], ['w1', 'chat-1'])
      expect(selectUnreadByMode('')(state)).toBe(1)
      expect(relayedUnreadKeys(state.dashboard)).toEqual(['chat-1'])
      expect(state.dashboard.unreadSlots).toEqual(['w1', 'chat-1'])
      // Its root closed, the crew member is a row of the list again, so it counts.
      const orphan: ChatSlot = { key: 'w2', title: 'W2', messages: 1, running: false, mode: '', crew_root: 'gone-root' }
      const closed = buildState([plain, orphan], ['w2', 'chat-1'])
      expect(selectUnreadByMode('')(closed)).toBe(2)
      expect(relayedUnreadKeys(closed.dashboard)).toEqual(['w2', 'chat-1'])
    })

    it('keeps an unheld orphan in the chat badge but leaves a held orphan out', () => {
      // An unheld orphan is about to be drained by the next reconcile, so it
      // keeps counting. A held one (a tab the restore could not reopen) is never
      // drained by a list, so counting it would show a session the sidebar
      // cannot display. Its unread entry stays, for when the tab comes back.
      const live: ChatSlot = { key: 'chat-1', title: 'C', messages: 0, running: false, mode: '' }
      const base = buildState([live], ['chat-1', 'chat-9-gone', 'chat-8-orphan'])
      const held = {
        dashboard: { ...base.dashboard, slotsHeldKeys: ['chat-9-gone'] },
      } as typeof base
      expect(selectUnreadByMode('')(base)).toBe(3)
      expect(selectUnreadByMode('')(held)).toBe(2)
      expect(held.dashboard.unreadSlots).toContain('chat-9-gone')
    })

    it('leaves channel orphans out of the badge while every channel key is held', () => {
      const unread = ['slack_1785370133.085469']
      const base = buildState([], unread)
      const holding = {
        dashboard: { ...base.dashboard, slotsHoldChannels: true },
      } as typeof base
      expect(selectUnreadByMode('')(base)).toBe(1)
      expect(selectUnreadByMode('')(holding)).toBe(0)
    })

    it('leaves every orphan out of the badge while the gateway labels its list partial', () => {
      // The gateway cannot name a tab it has not restored yet, so slotsHeldKeys
      // is empty mid-restore; the drain is still held, so the badge must skip it.
      const live: ChatSlot = { key: 'chat-1', title: 'C', messages: 0, running: false, mode: '' }
      let state = reducer(initial, sseSlotsPartial(true))
      state = reducer(state, sseSlots([live]))
      state = { ...state, unreadSlots: ['chat-1', 'chat-2-restoring'] }
      expect(selectUnreadByMode('')({ dashboard: state } as unknown as Parameters<ReturnType<typeof selectUnreadByMode>>[0])).toBe(1)
      // An unlabelled hold (boot, reconnect before the first frame) skips nothing,
      // so dots restored from storage still count.
      const booting = buildState([live], ['chat-1', 'chat-2-restoring'])
      expect(booting.dashboard.unreadDrainHeld).toBe(true)
      expect(selectUnreadByMode('')(booting)).toBe(2)
      // The label clears and the orphan counts again until it drains.
      state = reducer(state, sseSlotsPartial(false))
      expect(state.slotsListPartial).toBe(false)
    })

    it('counts a held key again once its slot is listed', () => {
      const back: ChatSlot = { key: 'chat-9-gone', title: 'B', messages: 0, running: false, mode: '' }
      const base = buildState([back], ['chat-9-gone'])
      const held = {
        dashboard: { ...base.dashboard, slotsHeldKeys: ['chat-9-gone'] },
      } as typeof base
      expect(selectUnreadByMode('')(held)).toBe(1)
    })

    it('returns the same selector instance on repeated calls (memoization)', () => {
      // Stable reference matters because consumers (selectSurfaceBadgeCount,
      // selectAllSurfacesAttention) call this on every render — recreating
      // the selector would defeat both useAppSelector's referential-equality
      // fast path and reselect's input-equality memoization.
      expect(selectUnreadByMode('dashboard')).toBe(selectUnreadByMode('dashboard'))
    })
  })

  describe('reconnect unread suppression', () => {
    // The useWebSocket hook uses a reconnectingRef (set directly in onopen,
    // cleared on fetchSlots resolve) to suppress markSlotUnread during the
    // post-reconnect catch-up window. These reducer-level tests verify the
    // store invariants the hook relies on; the actual guard is pinned by
    // useWebSocketReconnect.test.ts at the hook level.
    it('sseConnected resets slotsLoaded to false (reconnect signal)', () => {
      let state = reducer(initial, sseSlots([slot1, slot2]))
      expect(state.slotsLoaded).toBe(true)
      state = reducer(state, sseConnected())
      expect(state.slotsLoaded).toBe(false)
    })

    it('fetchSlots.fulfilled after reconnect does not spuriously add unreads', () => {
      // Simulates: reconnect → fetchSlots returns slots → no unreads added
      // (markSlotUnread is guarded by reconnectingRef in the hook, not reducer)
      let state = reducer(initial, markSlotUnread('chat-1'))
      state = reducer(state, sseConnected()) // reconnect
      state = reducer(state, fetchSlots.fulfilled([slot1, slot2], 'requestId'))
      // Existing unread preserved, no new ones spuriously added
      expect(state.unreadSlots).toEqual(['chat-1'])
      expect(state.slotsLoaded).toBe(true)
    })
  })

  /** One channel can carry TWO rows — the conversation a session was born in AND
   *  an explicit mirror to that same channel — and they disconnect independently.
   *  Matching on `channel` alone patched whichever row came first in the array, so
   *  acting on the mirror moved the origin row's `paused` instead. The row the user
   *  clicked never changed, which reads as a dead control: it renders connected and
   *  cannot be reconnected.
   */
  describe('patchSlotLink disambiguates two rows on one channel', () => {
    const twoDiscordRows = (): ChatSlot => ({
      key: 'chat-1',
      title: 'Chat 1',
      messages: 1,
      running: false,
      pending_approval: false,
      waiting_for_input: false,
      last_activity_ts: undefined,
      links: [
        { channel: 'discord', label: 'Discord', target: 'dm-1', direction: 'origin', live: true, paused: false },
        { channel: 'discord', label: 'Discord', target: 'chan-2', direction: 'out', live: true, paused: false },
      ],
    })
    const rows = (s: ReturnType<typeof reducer>) => s.slots[0].links!

    it('patches the mirror row and leaves the origin row alone', () => {
      let state = reducer(initial, sseSlots([twoDiscordRows()]))
      state = reducer(state, patchSlotLink({
        key: 'chat-1', channel: 'discord', origin: false, patch: { paused: true },
      }))
      expect(rows(state)[1].paused).toBe(true)
      expect(rows(state)[0].paused).toBe(false)
    })

    it('patches the origin row and leaves the mirror row alone', () => {
      let state = reducer(initial, sseSlots([twoDiscordRows()]))
      state = reducer(state, patchSlotLink({
        key: 'chat-1', channel: 'discord', origin: true, patch: { paused: true },
      }))
      expect(rows(state)[0].paused).toBe(true)
      expect(rows(state)[1].paused).toBe(false)
    })

    // Classified by origin-ness, not by equality against `direction`, so this
    // lands the same side here as the flag the endpoint was called with.
    it('treats a `both` row as the mirror, like the endpoint flag does', () => {
      const slot = twoDiscordRows()
      slot.links![1].direction = 'both'
      let state = reducer(initial, sseSlots([slot]))
      state = reducer(state, patchSlotLink({
        key: 'chat-1', channel: 'discord', origin: false, patch: { paused: true },
      }))
      expect(rows(state)[1].paused).toBe(true)
      expect(rows(state)[0].paused).toBe(false)
    })

    // Slack has exactly one row, so its callers omit the flag.
    it('falls back to channel-only matching when origin is omitted', () => {
      let state = reducer(initial, sseSlots([twoDiscordRows()]))
      state = reducer(state, patchSlotLink({
        key: 'chat-1', channel: 'discord', patch: { paused: true },
      }))
      expect(rows(state)[0].paused).toBe(true)
    })
  })

  describe('dropSlotLinks removes the rows of ONE binding in place', () => {
    const twoDiscordRows = (): ChatSlot => ({
      key: 'chat-1',
      title: 'Chat 1',
      messages: 1,
      running: false,
      pending_approval: false,
      waiting_for_input: false,
      last_activity_ts: undefined,
      slack_linked: true,
      slack_channel: 'C-1',
      slack_thread_ts: '1.2',
      links: [
        { channel: 'discord', label: 'Discord', target: 'dm-1', binding: 'b-o', direction: 'origin', live: true, paused: false },
        { channel: 'discord', label: 'Discord', target: 'chan-2', binding: 'b-a', direction: 'both', live: true, paused: true },
        { channel: 'telegram', label: 'Telegram', target: 'tg-1', binding: 'b-t', direction: 'out', live: true, paused: false },
        { channel: 'slack', label: 'Slack', target: 'C-1', binding: 'b-s', direction: 'out', live: true, paused: false },
      ],
    })
    const rows = (s: ReturnType<typeof reducer>) => s.slots[0].links!

    // The Unlink action's write: the named binding is gone server-side, the
    // origin row (the conversation the session was born in) is not a binding
    // anyone severed, and every other row is untouched.
    it('drops the rows carrying the named binding and keeps the origin row', () => {
      let state = reducer(initial, sseSlots([twoDiscordRows()]))
      state = reducer(state, dropSlotLinks({ key: 'chat-1', channel: 'discord', binding: 'b-a' }))
      expect(rows(state).map(l => `${l.channel}:${l.direction}`)).toEqual([
        'discord:origin', 'telegram:out', 'slack:out',
      ])
    })

    it('leaves a same-channel row with a DIFFERENT binding alone', () => {
      // The race the token exists for: Unlink A, another tab links B on the same
      // channel before A's response lands, B's slots push arrives first. The
      // server deleted exactly A, so a completion keyed on the channel would
      // erase B — a binding the server still holds — and the tab would read as
      // disconnected. Keyed on the binding, B stays and nothing is stamped.
      let state = reducer(initial, sseSlots([twoDiscordRows()]))
      const before = state
      state = reducer(state, dropSlotLinks({ key: 'chat-1', channel: 'discord', binding: 'b-gone' }))
      expect(rows(state)).toHaveLength(4)
      expect(state.slots).toEqual(before.slots)
    })

    it('clears the Slack fields only with the Slack thread row it matched', () => {
      let state = reducer(initial, sseSlots([twoDiscordRows()]))
      // A stale token for the thread: no row matches, the fields stay.
      state = reducer(state, dropSlotLinks({ key: 'chat-1', channel: 'slack', binding: 'b-stale' }))
      expect(state.slots[0].slack_linked).toBe(true)
      expect(state.slots[0].slack_channel).toBe('C-1')
      expect(rows(state)).toHaveLength(4)
      // The current token: the row goes and the fields go with it, one write.
      state = reducer(state, dropSlotLinks({ key: 'chat-1', channel: 'slack', binding: 'b-s' }))
      expect(rows(state).map(l => l.channel)).toEqual(['discord', 'discord', 'telegram'])
      expect(state.slots[0].slack_linked).toBe(false)
      expect(state.slots[0].slack_channel).toBeUndefined()
      expect(state.slots[0].slack_thread_ts).toBeUndefined()
    })

    it('is a no-op for a channel with no rows and for an unknown slot', () => {
      let state = reducer(initial, sseSlots([twoDiscordRows()]))
      const before = state
      state = reducer(state, dropSlotLinks({ key: 'chat-1', channel: 'imessage', binding: 'b-a' }))
      expect(rows(state)).toHaveLength(4)
      state = reducer(state, dropSlotLinks({ key: 'chat-404', channel: 'discord', binding: 'b-a' }))
      expect(state.slots).toEqual(before.slots)
    })
  })
})

describe('dashboardSlice per-slot unread teardown', () => {
  const seeded = () => {
    const base = reducer(undefined, { type: '@@INIT' })
    return {
      ...base,
      slots: [{ key: 'chat-1', messages: 0, running: false }, { key: 'chat-2', messages: 0, running: false }] as ChatSlot[],
      // Steady state: a complete slots frame has released the boot-time hold.
      unreadDrainHeld: false,
    }
  }

  it('drains unread state for a slot that vanished from the authoritative list', () => {
    // Persisted state lives in the ONE shared record; 'mc-unread-slots' is a
    // write-only projection of its keys. Seed the record the way arrivals do.
    localStorage.setItem('mc-unread-shared', JSON.stringify({ 'chat-1': '', 'chat-2': '' }))
    const before = { ...seeded(), unreadSlots: ['chat-1', 'chat-2'] }

    const next = reducer(before, sseSlots([{ key: 'chat-1', messages: 0, running: false }] as ChatSlot[]))

    expect(next.unreadSlots).toEqual(['chat-1'])
    expect(Object.keys(JSON.parse(sharedItem('mc-unread-shared') ?? '{}'))).toEqual(['chat-1'])
    expect(JSON.parse(sharedItem('mc-unread-slots') ?? '[]')).toEqual(['chat-1'])
  })

  it('leaves unread state alone when the frame still lists every unread slot', () => {
    localStorage.removeItem('mc-unread-slots')
    const before = { ...seeded(), unreadSlots: ['chat-1', 'chat-2'] }

    const next = reducer(before, sseSlots([
      { key: 'chat-1', messages: 0, running: false },
      { key: 'chat-2', messages: 0, running: false },
    ] as ChatSlot[]))

    expect(next.unreadSlots).toEqual(['chat-1', 'chat-2'])
    // Not rewritten, because this reducer runs on every slots frame.
    expect(sharedItem('mc-unread-slots')).toBeNull()
  })

  it('reconciles an empty frame once loaded, which is the last slot being deleted', () => {
    const loaded = { ...seeded(), slotsLoaded: true, unreadSlots: ['chat-1', 'chat-2'] }

    const next = reducer(loaded, sseSlots([]))

    expect(next.unreadSlots).toEqual([])
  })

  it('drains unread from a fetch reply once the stream is live', () => {
    // The reply can be older than the live frames it raced; the unread drain
    // still runs because it self-heals on the next unread event.
    const loaded = { ...seeded(), slotsLoaded: true, unreadSlots: ['chat-1', 'chat-2'] }
    const payload = [{ key: 'chat-1', messages: 0, running: false }] as ChatSlot[]

    const next = reducer(loaded, { type: fetchSlots.fulfilled.type, payload })

    expect(next.unreadSlots).toEqual(['chat-1'])
  })
})

// Last, after the file's own hooks that clear localStorage: Crew Mode is a feature preview.
crewModePreviewOn()
