import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { renderHook } from '@testing-library/react'
import { store } from '../../store'
import { sseSlots, markSlotRead, markSlotUnread, type SlotPatchFrame } from '../../store/dashboardSlice'
import { setActiveSlot } from '../../store/chatSlice'
import { MANUAL_UNREAD, __resetSharedUnreadQueueForTests, flushSharedUnread } from '../../store/dashboard/unread'
import type { ChatSlot } from '../../types'
import { applyServerReads } from './serverReads'
import { bindSlotReadSender, emitSlotRead, noteShownHere, _resetSlotReadRelayForTest } from '../../lib/slotReadRelay'
import { queueRead, queuedReads } from '../../lib/readOutbox'
import { turnOffCrewModeAsLts } from '../../test/crewModeOff'
import { UNREAD_ON_ATTENTION_KEY } from '../unreadOnAttention'
import { useSlotListSync } from './slotList'
import type { StreamBuffers } from './streamBuffers'

const EPOCH = '2026-11-13T08:00:00+00:00'
const READ = '2026-11-13T10:00:00+00:00'
const BEFORE = '2026-11-13T09:00:00+00:00'
const AFTER = '2026-11-13T11:00:00+00:00'

const row = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({ key, title: key, messages: 1, running: false, last_read_ts: READ, read_epoch: EPOCH, unread_ts: AFTER, last_ts: AFTER, ...extra } as ChatSlot)

const queryClient = new QueryClient()
const sent: Array<[string, string | undefined]> = []

const apply = (rows: ChatSlot[]) => {
  store.dispatch(sseSlots(rows))
  applyServerReads(store.getState().dashboard.slots, { dispatch: store.dispatch, queryClient })
}
const buffers = { hasPendingChunks: () => false, flushChunks: () => {} } as unknown as StreamBuffers
const slotListSync = () => renderHook(() => useSlotListSync(store.dispatch, queryClient, buffers)).result.current
const unread = () => store.getState().dashboard.unreadSlots

describe('applyServerReads', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    __resetSharedUnreadQueueForTests()
    _resetSlotReadRelayForTest()
    sent.length = 0
    bindSlotReadSender((slot, ts) => { sent.push([slot, ts]) })
    queryClient.clear()
    store.dispatch(setActiveSlot(null))
    for (const key of [...store.getState().dashboard.unreadSlots]) store.dispatch(markSlotRead(key))
  })
  afterEach(() => { store.dispatch(sseSlots([])); _resetSlotReadRelayForTest() })

  it('badges a session whose agent wrote after the last read, with no live frame', () => {
    apply([row('missed')])
    expect(unread()).toContain('missed')
    expect(store.getState().dashboard.unreadSince?.missed).toBe(AFTER)
  })

  it('badges nothing a read covers, nor anything before the gateway has read its store', () => {
    apply([row('seen', { unread_ts: BEFORE }), row('unknown', { last_read_ts: null, read_epoch: null })])
    expect(unread()).toEqual([])
  })

  describe('the store epoch', () => {
    const restored = (key: string) => {
      store.dispatch(markSlotUnread({ slot: key, ts: BEFORE }))
      flushSharedUnread()
      expect(unread()).toContain(key)
      return localStorage.getItem('mc-unread-shared')
    }

    it('never clears a restored badge, though it is newer', () => {
      const shared = restored('upgraded')
      apply([row('upgraded', { last_read_ts: null, read_epoch: READ, unread_ts: BEFORE })])
      expect(unread()).toContain('upgraded')
      expect(localStorage.getItem('mc-unread-shared')).toBe(shared)
    })

    it('lets a real read at that time clear the same badge', () => {
      restored('read')
      apply([row('read', { last_read_ts: READ, read_epoch: READ, unread_ts: BEFORE })])
      expect(unread()).not.toContain('read')
    })

    it('badges, on a slot nobody read, only a reply after it', () => {
      apply([row('old', { last_read_ts: null, read_epoch: READ, unread_ts: BEFORE }), row('new', { last_read_ts: null, read_epoch: READ, unread_ts: AFTER })])
      expect(unread()).toEqual(['new'])
    })
  })

  it('skips the slot on screen, a muted slot, a crewmate thread, a running turn and a note after the turn under the attention setting', () => {
    store.dispatch(setActiveSlot('open'))
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    apply([row('open'), row('muted', { muted: true }), row('running', { running: true }), row('done', { last_agent_ts: AFTER }),
      row('member-mate', { mode: 'member', unread_ts: BEFORE }), row('member-busy', { mode: 'member', running: true }),
      row('note', { last_agent_ts: BEFORE }), row('failed', { last_agent_ts: BEFORE, unread_failed: true })])
    expect(unread()).toEqual(['done', 'failed'])
  })

  it('badges a crewmate reply, and a terminal failure in a muted session once mutes are known', () => {
    queryClient.setQueryData(['kirocrewConfig'], {})
    apply([row('mate', { mode: 'member' }), row('failed', { muted: true, unread_failed: true })])
    expect(unread()).toEqual(['mate', 'failed'])
  })

  it('badges a held app session once the app mutes load, with no new slot list', () => {
    slotListSync().onSlots({}, [row('app-run', { app: 'notes' })], 'frame')
    expect(unread()).not.toContain('app-run')
    queryClient.setQueryData(['kirocrewConfig'], { apps: { notes: {} } })
    expect(unread()).toContain('app-run')
  })

  it('badges a running turn that waits on the user under the attention setting', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    apply([row('approval', { running: true, pending_approval: true }), row('question', { running: true, needs_input: true })])
    expect(unread()).toEqual(['approval', 'question'])
  })

  describe('a crew member waiting on an approval', () => {
    const crew = (extra: Partial<ChatSlot> = {}) => [row('conductor', { unread_ts: BEFORE }),
      row('worker', { crew_root: 'conductor', running: true, pending_approval: true, last_agent_ts: AFTER, ...extra })]

    it('badges its conductor and itself', () => {
      apply(crew())
      expect(unread()).toEqual(['conductor', 'worker'])
      expect(store.getState().dashboard.unreadSince?.conductor).toBe(AFTER)
    })

    it('leaves the conductor alone while it is on screen or already read past the row', () => {
      store.dispatch(setActiveSlot('conductor'))
      apply(crew())
      expect(unread()).toEqual(['worker'])
      store.dispatch(setActiveSlot(null))
      for (const key of [...unread()]) store.dispatch(markSlotRead(key))
      apply([row('conductor', { last_read_ts: AFTER, unread_ts: BEFORE }), crew()[1]])
      expect(unread()).toEqual(['worker'])
    })

    it('stays clear after the conductor is read in a window that never saw the prompt arrive', () => {
      apply(crew())
      expect(unread()).toContain('conductor')
      emitSlotRead('conductor', BEFORE)
      const readTs = sent.find(([slot]) => slot === 'conductor')?.[1]
      for (const key of [...unread()]) store.dispatch(markSlotRead(key))
      apply([row('conductor', { last_read_ts: readTs, unread_ts: BEFORE }), crew()[1]])
      expect(unread()).toEqual(['worker'])
    })

    it('badges only the worker with Crew Mode off', () => {
      turnOffCrewModeAsLts()
      apply(crew())
      expect(unread()).toEqual(['worker'])
    })

    it('badges no conductor for a worker that is not waiting on an approval', () => {
      apply(crew({ pending_approval: false }))
      expect(unread()).toEqual(['worker'])
    })
  })

  it('clears a badge a read on another device covers', () => {
    apply([row('elsewhere', { last_read_ts: BEFORE })])
    expect(unread()).toContain('elsewhere')
    apply([row('elsewhere', { last_read_ts: AFTER })])
    expect(unread()).not.toContain('elsewhere')
  })

  it('keeps a badge for a message newer than the read', () => {
    apply([row('newer', { last_read_ts: BEFORE })])
    store.dispatch(markSlotUnread({ slot: 'newer', ts: '2026-11-13T12:00:00+00:00' }))
    apply([row('newer', { last_read_ts: AFTER })])
    expect(unread()).toContain('newer')
  })

  it('re-badges a reply newer than the read that cleared the old badge', () => {
    apply([row('replied', { last_read_ts: BEFORE, unread_ts: READ })])
    expect(store.getState().dashboard.unreadSince?.replied).toBe(READ)
    apply([row('replied', { last_read_ts: READ, unread_ts: AFTER })])
    expect(unread()).toContain('replied')
    expect(store.getState().dashboard.unreadSince?.replied).toBe(AFTER)
  })

  it('never clears a manual mark-as-unread', () => {
    apply([row('manual', { unread_ts: BEFORE })])
    store.dispatch(markSlotUnread('manual'))
    expect(store.getState().dashboard.unreadSince?.manual).toBe(MANUAL_UNREAD)
    apply([row('manual', { last_read_ts: AFTER, unread_ts: BEFORE })])
    expect(unread()).toContain('manual')
  })

  it('counts a row this window showed on screen as read', () => {
    noteShownHere('watched', AFTER)
    apply([row('watched')])
    expect(unread()).not.toContain('watched')
  })

  describe('a read the gateway has not confirmed', () => {
    it('holds the badge off, and no slot list sends it', () => {
      queueRead('laggy', AFTER)
      slotListSync().onSlots({ type: 'slots' }, [row('laggy')], 'laggy-1')
      expect(unread()).not.toContain('laggy')
      slotListSync().onSlots({ type: 'slots' }, [row('laggy', { title: 'again' })], 'laggy-2')
      expect(sent).toEqual([])
    })

    it('is forgotten once a slot row carries a read at least as new', () => {
      queueRead('confirmed', READ)
      apply([row('confirmed', { last_read_ts: READ, unread_ts: BEFORE })])
      expect(queuedReads()).toEqual({})
      slotListSync().resetForConnection()
      expect(sent).toEqual([])
    })
  })

  it('never badges a slot_patch frame from the older read and agent times its rows keep', () => {
    store.dispatch(sseSlots([row('echoed', { last_read_ts: BEFORE })]))
    slotListSync().onSlotPatch({ slots: [{ key: 'echoed', title: 'renamed' }] } as SlotPatchFrame)
    expect(unread()).not.toContain('echoed')
  })
})
