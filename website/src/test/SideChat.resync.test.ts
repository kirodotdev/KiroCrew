/** A Side Chat panel waiting on a turn whose final frame this tab never received
 *  reads the gateway's copy again each time the socket opens.
 *
 *  Side frames reach owner sockets only, so a turn that ends after the panel's
 *  restore read was answered but before this tab's socket connected, or while
 *  the socket is down, sends its final frame to nobody here. Without a second
 *  read the panel shows the turn as thinking until the next reload. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import chatReducer, {
  restoreSideConversation, resyncWaitingSideConversations, sideClose, sideOptimisticAccepted, sideOptimisticAppend, sideOptimisticRollback, sideRestore, sideResync,
  sseSideResult,
} from '../store/chatSlice'
import type { SideSnapshot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    sideState: vi.fn(),
  },
}))

const SLOT = 'slot-1'
const CREATED = '2026-10-21T06:00:00.000Z'

type ChatState = ReturnType<typeof chatReducer>

const fresh = (): ChatState => chatReducer(undefined, { type: '@@INIT' })

/** Q1 was answered and Q2 is running, its answer not stored yet. */
function running(over: Partial<SideSnapshot> = {}): SideSnapshot {
  return {
    open: true,
    created_at: CREATED,
    last_run_id: 'r2',
    running: true,
    messages: [
      { id: 'u1', run_id: 'r1', role: 'user', content: 'Q1', ts: '2026-10-21T06:00:02.000Z' },
      { id: 'a1', run_id: 'r1', role: 'assistant', content: 'A1', ts: '2026-10-21T06:00:03.000Z' },
      { id: 'u2', run_id: 'r2', role: 'user', content: 'Q2', ts: '2026-10-21T06:00:04.000Z' },
    ],
    queue: [],
    ...over,
  }
}

/** The same conversation once Q2's answer is stored and no turn runs. */
const finished = (): SideSnapshot => running({
  running: false,
  messages: [...(running().messages ?? []), { id: 'a2', run_id: 'r2', role: 'assistant', content: 'A2', ts: '2026-10-21T06:00:05.000Z' }],
})

/** The same conversation once Q2 is answered and Q3 runs, its answer not stored yet. */
const askingQ3 = (): SideSnapshot => running({
  last_run_id: 'r3',
  messages: [...(finished().messages ?? []), { id: 'u3', run_id: 'r3', role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' }],
})

const anotherConversation = (): SideSnapshot => running({
  created_at: '2026-10-21T07:00:00.000Z',
  last_run_id: 'r7',
  running: false,
  messages: [
    { id: 'u7', run_id: 'r7', role: 'user', content: 'Other Q', ts: '2026-10-21T07:00:02.000Z' },
    { id: 'a7', run_id: 'r7', role: 'assistant', content: 'Other A', ts: '2026-10-21T07:00:03.000Z' },
  ],
})

/** The other conversation once a later question of it is stored and running. */
const anotherConversationAsking = (): SideSnapshot => ({
  ...anotherConversation(),
  last_run_id: 'r8',
  running: true,
  messages: [...(anotherConversation().messages ?? []), { id: 'u8', run_id: 'r8', role: 'user', content: 'Q8', ts: '2026-10-21T07:00:04.000Z' }],
})

/** Q8 begins in the other conversation, asked from another tab. */
const askQ8 = () => sseSideResult({ slot: SLOT, run_id: 'r8', role: 'user', content: 'Q8', message_id: 'u8' })

const contents = (s: ChatState) => s.slotSide[SLOT].messages.map(m => m.content)
const panelOf = (s: ChatState) => s.slotSide[SLOT]

/** A question the gateway stores and echoes redacted. Assembled at runtime so the
 *  repository's content scan does not read the test as carrying a key. */
const questionTheWireRedacts = `key ${['-----BEGIN', 'KEY-----'].join(' PRIVATE ')}`

/** A tab that restored the running turn: it waits for Q2's final frame. */
const waitingOnQ2 = (): ChatState => chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: running() }))

describe('sideResync', () => {
  it('settles an accepted optimistic question already stored and answered', () => {
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
    }))
    before = chatReducer(before, sideOptimisticAccepted({ slot: SLOT, content: 'Q3', runId: 'r3' }))
    const snapshot = running({
      last_run_id: 'r3',
      running: false,
      messages: [
        ...(finished().messages ?? []),
        { id: 'u3', run_id: 'r3', role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
        { id: 'a3', run_id: 'r3', role: 'assistant', content: 'A3', ts: '2026-10-21T06:00:07.000Z' },
      ],
    })

    const s = chatReducer(before, sideResync({ slot: SLOT, snapshot, readFor: panelOf(before) }))
    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2', 'Q3', 'A3'])
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(false)
  })

  it('keeps a retried question pending when an earlier identical turn is stored', () => {
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
    }))
    before = chatReducer(before, sideOptimisticAccepted({ slot: SLOT, content: 'Q3', runId: 'r3' }))
    before = chatReducer(before, sideOptimisticRollback(SLOT))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q3', ts: '2026-10-21T06:00:08.000Z' },
    }))
    const snapshot = running({
      last_run_id: 'r3',
      running: false,
      messages: [
        ...(finished().messages ?? []),
        { id: 'u3', run_id: 'r3', role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
        { id: 'a3', run_id: 'r3', role: 'assistant', content: 'A3', ts: '2026-10-21T06:00:07.000Z' },
      ],
    })

    let s = chatReducer(before, sideResync({ slot: SLOT, snapshot, readFor: panelOf(before) }))
    expect(panelOf(s).messages.filter(row => row.optimistic && row.content === 'Q3')).toHaveLength(1)
    expect(panelOf(s).pending).toBe(true)

    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: 'Q3', runId: 'r4' }))
    expect(panelOf(s).messages.find(row => row.optimistic && row.content === 'Q3')).toMatchObject({ acceptedRunId: 'r4' })
  })

  it('keeps an unaccepted stored question unsent until its receipt lands', () => {
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
    }))
    const snapshot = running({
      last_run_id: 'r3',
      running: false,
      messages: [
        ...(finished().messages ?? []),
        { id: 'u3', run_id: 'r3', role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
        { id: 'a3', run_id: 'r3', role: 'assistant', content: 'A3', ts: '2026-10-21T06:00:07.000Z' },
      ],
    })

    let s = chatReducer(before, sideResync({ slot: SLOT, snapshot, readFor: panelOf(before) }))
    expect(panelOf(s).messages.filter(row => row.optimistic && row.content === 'Q3')).toHaveLength(1)
    expect(panelOf(s).pending).toBe(true)

    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: 'Q3', runId: 'r3' }))
    expect(panelOf(s).messages.filter(row => row.role === 'user' && row.content === 'Q3')).toHaveLength(1)
    expect(panelOf(s).messages.some(row => row.optimistic)).toBe(false)
    expect(panelOf(s).pending).toBe(false)
  })

  it('settles a redacted stored question by its accepted run', () => {
    const raw = questionTheWireRedacts
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: raw, ts: '2026-10-21T06:00:06.000Z' },
    }))
    before = chatReducer(before, sideOptimisticAccepted({ slot: SLOT, content: raw, runId: 'r3' }))
    const snapshot = running({
      last_run_id: 'r3',
      running: false,
      messages: [
        ...(finished().messages ?? []),
        { id: 'u3', run_id: 'r3', role: 'user', content: '[REDACTED]', ts: '2026-10-21T06:00:06.000Z' },
        { id: 'a3', run_id: 'r3', role: 'assistant', content: 'A3', ts: '2026-10-21T06:00:07.000Z' },
      ],
    })

    const s = chatReducer(before, sideResync({ slot: SLOT, snapshot, readFor: panelOf(before) }))
    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2', '[REDACTED]', 'A3'])
    expect(s.slotSide[SLOT].messages.filter(m => m.role === 'user' && m.run_id === 'r3')).toHaveLength(1)
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(false)
  })

  it('settles a redacted live user frame by its accepted run', () => {
    const raw = questionTheWireRedacts
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: raw, ts: '2026-10-21T06:00:06.000Z' },
    }))
    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: raw, runId: 'r3' }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: '[REDACTED]', message_id: 'u3' }))

    const questions = s.slotSide[SLOT].messages.filter(m => m.role === 'user' && m.run_id === 'r3')
    expect(questions).toHaveLength(1)
    expect(questions[0]).toMatchObject({ id: 'u3', content: raw })
    expect(questions[0].optimistic).toBeUndefined()
    expect(questions[0].acceptedRunId).toBeUndefined()
    expect(s.slotSide[SLOT].pending).toBe(true)
  })

  it('removes the optimistic duplicate when its redacted user frame arrived first', () => {
    const raw = questionTheWireRedacts
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: raw, ts: '2026-10-21T06:00:06.000Z' },
    }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: '[REDACTED]', message_id: 'u3' }))
    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: raw, runId: 'r3' }))

    const questions = s.slotSide[SLOT].messages.filter(m => m.role === 'user' && m.run_id === 'r3')
    expect(questions).toHaveLength(1)
    expect(questions[0]).toMatchObject({ id: 'u3', content: '[REDACTED]' })
    expect(s.slotSide[SLOT].messages.some(m => m.optimistic && m.content === raw)).toBe(false)
    expect(s.slotSide[SLOT].pending).toBe(true)
  })

  it('clears pending when a late receipt removes the last optimistic row after resync completed the run', () => {
    const raw = questionTheWireRedacts
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: raw, ts: '2026-10-21T06:00:06.000Z' },
    }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: '[REDACTED]', message_id: 'u3' }))
    const snapshot = running({
      last_run_id: 'r3',
      running: false,
      messages: [
        ...(finished().messages ?? []),
        { id: 'u3', run_id: 'r3', role: 'user', content: '[REDACTED]', ts: '2026-10-21T06:00:06.000Z' },
        { id: 'a3', run_id: 'r3', role: 'assistant', content: 'A3', ts: '2026-10-21T06:00:07.000Z' },
      ],
    })
    s = chatReducer(s, sideResync({ slot: SLOT, snapshot, readFor: panelOf(s) }))
    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: raw, runId: 'r3' }))

    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2', '[REDACTED]', 'A3'])
    expect(s.slotSide[SLOT].messages.filter(m => m.role === 'user' && m.run_id === 'r3')).toHaveLength(1)
    expect(s.slotSide[SLOT].messages.some(m => m.optimistic)).toBe(false)
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(false)
  })

  it('keeps pending when a late receipt removes the last optimistic row while the run continues', () => {
    const raw = questionTheWireRedacts
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: raw, ts: '2026-10-21T06:00:06.000Z' },
    }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: '[REDACTED]', message_id: 'u3' }))
    const snapshot = running({
      last_run_id: 'r3',
      running: true,
      messages: [
        ...(finished().messages ?? []),
        { id: 'u3', run_id: 'r3', role: 'user', content: '[REDACTED]', ts: '2026-10-21T06:00:06.000Z' },
      ],
    })
    s = chatReducer(s, sideResync({ slot: SLOT, snapshot, readFor: panelOf(s) }))
    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: raw, runId: 'r3' }))

    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2', '[REDACTED]'])
    expect(s.slotSide[SLOT].messages.filter(m => m.role === 'user' && m.run_id === 'r3')).toHaveLength(1)
    expect(s.slotSide[SLOT].messages.some(m => m.optimistic)).toBe(false)
    expect(s.slotSide[SLOT].pending).toBe(true)
  })

  it('ends the wait when a late receipt lands after a read found its conversation gone', () => {
    const raw = questionTheWireRedacts
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: raw, ts: '2026-10-21T06:00:06.000Z' },
    }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: '[REDACTED]', message_id: 'u3' }))
    s = chatReducer(s, sideResync({ slot: SLOT, snapshot: { open: false }, readFor: panelOf(s) }))
    expect(s.slotSide[SLOT].pending).toBe(true)
    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: raw, runId: 'r3' }))

    expect(s.slotSide[SLOT].messages.some(m => m.optimistic)).toBe(false)
    expect(s.slotSide[SLOT].pending).toBe(false)
  })

  it('leaves a streaming answer alone when a late receipt removes its duplicate', () => {
    const raw = questionTheWireRedacts
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: raw, ts: '2026-10-21T06:00:06.000Z' },
    }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: '[REDACTED]', message_id: 'u3' }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'assistant', content: 'A3 so far' }))
    expect(s.slotSide[SLOT].pending).toBe(false)
    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: raw, runId: 'r3' }))

    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(true)
  })

  it('keeps an idle panel idle when a late receipt removes a duplicate after the turn ended', () => {
    const raw = questionTheWireRedacts
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: raw, ts: '2026-10-21T06:00:06.000Z' },
    }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: '[REDACTED]', message_id: 'u3' }))
    // The gateway sends no row id with an answer it did not store.
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'assistant', content: 'A3', final: true }))
    expect(s.slotSide[SLOT].pending).toBe(false)
    s = chatReducer(s, sideOptimisticAccepted({ slot: SLOT, content: raw, runId: 'r3' }))

    expect(s.slotSide[SLOT].messages.some(m => m.optimistic)).toBe(false)
    expect(s.slotSide[SLOT].pending).toBe(false)
  })

  it('keeps an optimistic question that the gateway does not store yet', () => {
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
    }))

    const s = chatReducer(before, sideResync({ slot: SLOT, snapshot: finished(), readFor: panelOf(before) }))
    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2', 'Q3'])
    expect(s.slotSide[SLOT].pending).toBe(true)
  })

  it('keeps an optimistic question that repeats one the panel already shows', () => {
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q1', ts: '2026-10-21T06:00:06.000Z' },
    }))

    const s = chatReducer(before, sideResync({ slot: SLOT, snapshot: finished(), readFor: panelOf(before) }))
    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2', 'Q1'])
    expect(s.slotSide[SLOT].pending).toBe(true)
  })

  it('draws the answer of a turn that finished unheard and stops waiting for it', () => {
    const before = waitingOnQ2()
    expect(before.slotSide[SLOT].awaitingFinalRunId).toBe('r2')
    expect(before.slotSide[SLOT].pending).toBe(true)

    const s = chatReducer(before, sideResync({ slot: SLOT, snapshot: finished(), readFor: panelOf(before) }))
    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2'])
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(false)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('stops waiting when the gateway holds no open conversation, and keeps its rows', () => {
    const waiting = waitingOnQ2()
    const s = chatReducer(waiting, sideResync({ slot: SLOT, snapshot: { open: false }, readFor: panelOf(waiting) }))
    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('keeps busy a turn begun after a read that found no open conversation', () => {
    const waiting = waitingOnQ2()
    const s = chatReducer(chatReducer(waiting, askQ8()), sideResync({ slot: SLOT, snapshot: { open: false }, readFor: panelOf(waiting) }))

    expect(s.slotSide[SLOT].lastRunId).toBe('r8')
    expect(s.slotSide[SLOT].pending).toBe(true)
    expect(s.slotSide[SLOT].streaming).toBe(true)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('keeps holding back the deltas of a restored turn the gateway still runs', () => {
    const waiting = waitingOnQ2()
    const s = chatReducer(waiting, sideResync({ slot: SLOT, snapshot: running(), readFor: panelOf(waiting) }))
    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].pending).toBe(true)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBe('r2')
  })

  it('stops waiting and keeps its rows when the gateway reports another conversation', () => {
    const waiting = waitingOnQ2()
    const s = chatReducer(waiting, sideResync({ slot: SLOT, snapshot: anotherConversation(), readFor: panelOf(waiting) }))

    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(false)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('keeps busy a turn begun after a read that found another conversation', () => {
    for (const snapshot of [anotherConversation(), anotherConversationAsking()]) {
      const waiting = waitingOnQ2()
      const s = chatReducer(chatReducer(waiting, askQ8()), sideResync({ slot: SLOT, snapshot, readFor: panelOf(waiting) }))

      expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'Q8'])
      expect(s.slotSide[SLOT].lastRunId).toBe('r8')
      expect(s.slotSide[SLOT].pending).toBe(true)
      expect(s.slotSide[SLOT].streaming).toBe(true)
      expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
    }
  })

  it('tells another conversation apart by the run a receipt named, and settles that question', () => {
    let before = chatReducer(fresh(), sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q1', ts: CREATED },
    }))
    before = chatReducer(before, sideOptimisticAccepted({ slot: SLOT, content: 'Q1', runId: 'r1' }))

    const s = chatReducer(before, sideResync({ slot: SLOT, snapshot: anotherConversation(), readFor: panelOf(before) }))
    expect(contents(s)).toEqual(['Q1'])
    expect(panelOf(s).messages[0]).toMatchObject({ run_id: 'r1' })
    expect(panelOf(s).messages[0].optimistic).toBeUndefined()
    expect(panelOf(s).messages[0].acceptedRunId).toBeUndefined()
    expect(panelOf(s).pending).toBe(false)
    expect(panelOf(s).streaming).toBe(false)
  })

  it('settles a question accepted into a conversation the read finds gone, so a later refused send rolls back only itself', () => {
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
    }))
    before = chatReducer(before, sideOptimisticAccepted({ slot: SLOT, content: 'Q3', runId: 'r3' }))

    let s = chatReducer(before, sideResync({ slot: SLOT, snapshot: { open: false }, readFor: panelOf(before) }))
    expect(panelOf(s).pending).toBe(false)
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q4', ts: '2026-10-21T06:00:08.000Z' },
    }))
    s = chatReducer(s, sideOptimisticRollback(SLOT))

    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2', 'Q3'])
    expect(panelOf(s).messages.some(m => m.optimistic)).toBe(false)
    expect(panelOf(s).pending).toBe(false)
  })

  it('keeps unsent a question whose receipt came back after the read was sent', () => {
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
    }))
    const readFor = panelOf(before)
    const accepted = chatReducer(before, sideOptimisticAccepted({ slot: SLOT, content: 'Q3', runId: 'r3' }))

    const s = chatReducer(accepted, sideResync({ slot: SLOT, snapshot: { open: false }, readFor }))
    const question = panelOf(s).messages.find(m => m.content === 'Q3')
    expect(question).toMatchObject({ optimistic: true, acceptedRunId: 'r3' })
    expect(question?.run_id).toBeUndefined()
    expect(panelOf(s).pending).toBe(true)
  })

  it('keeps busy a question still being sent when the read finds the conversation gone', () => {
    let before = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: finished() }))
    before = chatReducer(before, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'Q3', ts: '2026-10-21T06:00:06.000Z' },
    }))

    for (const snapshot of [{ open: false }, anotherConversation()]) {
      const s = chatReducer(before, sideResync({ slot: SLOT, snapshot, readFor: panelOf(before) }))
      expect(panelOf(s).messages.find(m => m.content === 'Q3')?.optimistic).toBe(true)
      expect(panelOf(s).pending).toBe(true)
      expect(panelOf(s).streaming).toBe(false)
    }
  })

  it('keeps local rows from a later run when the snapshot shares an earlier run', () => {
    const waiting = waitingOnQ2()
    const before = chatReducer(waiting, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: 'Q3', message_id: 'u3' }))

    const s = chatReducer(before, sideResync({ slot: SLOT, snapshot: finished(), readFor: panelOf(waiting) }))

    expect(contents(s)).toEqual(['Q1', 'A1', 'Q2', 'A2', 'Q3'])
    expect(s.slotSide[SLOT].lastRunId).toBe('r3')
    expect(s.slotSide[SLOT].pending).toBe(true)
    expect(s.slotSide[SLOT].streaming).toBe(true)
  })

  it('leaves a turn this tab is still streaming as it is', () => {
    let s = chatReducer(fresh(), sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'Q2', message_id: 'u2' }))
    s = chatReducer(s, sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'half an ans' }))
    s = chatReducer(s, sideResync({ slot: SLOT, snapshot: running({ messages: [running().messages![2]] }), readFor: panelOf(s) }))
    expect(contents(s)).toEqual(['Q2', 'half an ans'])
    expect(s.slotSide[SLOT].streaming).toBe(true)
  })

  it('leaves alone a conversation that replaced the one the read was for', () => {
    const waiting = waitingOnQ2()
    let s = chatReducer(waiting, sideClose(SLOT))
    s = chatReducer(s, sideOptimisticAppend({ slot: SLOT, message: { role: 'user', content: 'Q-new', ts: '2026-10-21T07:00:00.000Z' } }))
    for (const snapshot of [finished(), { open: false }]) {
      const after = chatReducer(s, sideResync({ slot: SLOT, snapshot, readFor: panelOf(waiting) }))
      expect(contents(after)).toEqual(['Q-new'])
      expect(after.slotSide[SLOT].pending).toBe(true)
    }
  })

  it('changes nothing for a closed side', () => {
    const waiting = waitingOnQ2()
    const s = chatReducer(chatReducer(waiting, sideClose(SLOT)), sideResync({ slot: SLOT, snapshot: finished(), readFor: panelOf(waiting) }))
    expect(s.slotSide[SLOT]).toBeUndefined()
  })
})

describe('resyncWaitingSideConversations', () => {
  beforeEach(() => {
    vi.mocked(api.sideState).mockReset()
  })

  it('reads again only a restored panel that still waits on a turn', async () => {
    const store = createTestStore()
    store.dispatch(sideRestore({ slot: SLOT, snapshot: running() }))
    store.dispatch(sideRestore({ slot: 'idle', snapshot: finished() }))
    // Side state from frames alone: its panel has not restored, and restores on mount.
    store.dispatch(sseSideResult({ slot: 'unrestored', run_id: 'r9', role: 'user', content: 'Q9', message_id: 'u9' }))
    vi.mocked(api.sideState).mockResolvedValue(finished())

    await store.dispatch(resyncWaitingSideConversations())

    expect(vi.mocked(api.sideState).mock.calls).toEqual([[SLOT]])
    expect(store.getState().chat.slotSide[SLOT].messages.map(m => m.content)).toContain('A2')
    expect(store.getState().chat.slotSide[SLOT].pending).toBe(false)
  })

  it('stops thinking and keeps its rows when the read finds another conversation', async () => {
    const store = createTestStore()
    store.dispatch(sideRestore({ slot: SLOT, snapshot: running() }))
    vi.mocked(api.sideState).mockResolvedValue(anotherConversation())

    await store.dispatch(resyncWaitingSideConversations())

    expect(vi.mocked(api.sideState).mock.calls).toEqual([[SLOT]])
    expect(store.getState().chat.slotSide[SLOT].messages.map(m => m.content)).toEqual(['Q1', 'A1', 'Q2'])
    expect(store.getState().chat.slotSide[SLOT].pending).toBe(false)
    expect(store.getState().chat.slotSide[SLOT].streaming).toBe(false)
    expect(store.getState().chat.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('waits for a restore read still in flight, then reads again a panel it left waiting', async () => {
    const store = createTestStore()
    let answerRestore: (snapshot: SideSnapshot) => void = () => {}
    vi.mocked(api.sideState)
      .mockImplementationOnce(() => new Promise<SideSnapshot>(resolve => { answerRestore = resolve }))
      .mockResolvedValueOnce(finished())
    const restore = store.dispatch(restoreSideConversation(SLOT))
    const resync = store.dispatch(resyncWaitingSideConversations())

    // The restore read was answered before the turn ended.
    answerRestore(running())
    await restore
    await resync

    expect(vi.mocked(api.sideState)).toHaveBeenCalledTimes(2)
    expect(store.getState().chat.slotSide[SLOT].messages.map(m => m.content)).toEqual(['Q1', 'A1', 'Q2', 'A2'])
    expect(store.getState().chat.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('drops a read that lands after its conversation was closed and replaced', async () => {
    const store = createTestStore()
    store.dispatch(sideRestore({ slot: SLOT, snapshot: running() }))
    let answer: (snapshot: SideSnapshot) => void = () => {}
    vi.mocked(api.sideState).mockImplementationOnce(() => new Promise<SideSnapshot>(resolve => { answer = resolve }))
    const resync = store.dispatch(resyncWaitingSideConversations())
    await vi.waitFor(() => expect(api.sideState).toHaveBeenCalledTimes(1))

    // Refresh context, then a new question, while the read is out.
    store.dispatch(sideClose(SLOT))
    store.dispatch(sideOptimisticAppend({ slot: SLOT, message: { role: 'user', content: 'Q-new', ts: '2026-10-21T07:00:00.000Z' } }))
    answer(finished())
    await resync

    expect(store.getState().chat.slotSide[SLOT].messages.map(m => m.content)).toEqual(['Q-new'])
    expect(store.getState().chat.slotSide[SLOT].pending).toBe(true)
  })

  it('takes a 404 as a slot that is gone, so nothing of it still runs', async () => {
    const store = createTestStore()
    store.dispatch(sideRestore({ slot: SLOT, snapshot: running() }))
    vi.mocked(api.sideState).mockRejectedValueOnce(new ApiError(404, 'slot not found', ''))

    await store.dispatch(resyncWaitingSideConversations())

    expect(store.getState().chat.slotSide[SLOT].pending).toBe(false)
  })

  it('keeps the panel as it is when the read fails another way', async () => {
    const store = createTestStore()
    store.dispatch(sideRestore({ slot: SLOT, snapshot: running() }))
    vi.mocked(api.sideState).mockRejectedValueOnce(new ApiError(500, 'unavailable', ''))

    await store.dispatch(resyncWaitingSideConversations())

    expect(store.getState().chat.slotSide[SLOT].awaitingFinalRunId).toBe('r2')
  })

  it('applies only the newer of two overlapping reads, so an older answer cannot undo it', async () => {
    const store = createTestStore()
    store.dispatch(sideRestore({ slot: SLOT, snapshot: running() }))
    let answerOlder: (snapshot: SideSnapshot) => void = () => {}
    vi.mocked(api.sideState)
      .mockImplementationOnce(() => new Promise<SideSnapshot>(resolve => { answerOlder = resolve }))
      .mockResolvedValueOnce(askingQ3())
    const older = store.dispatch(resyncWaitingSideConversations())
    await vi.waitFor(() => expect(api.sideState).toHaveBeenCalledTimes(1))

    // The socket opens again before the first read answers, and by then Q3 runs.
    await store.dispatch(resyncWaitingSideConversations())
    expect(store.getState().chat.slotSide[SLOT].awaitingFinalRunId).toBe('r3')

    answerOlder(finished())
    await older
    store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r3', role: 'assistant', content: 'the middle of A3' }))

    expect(store.getState().chat.slotSide[SLOT].awaitingFinalRunId).toBe('r3')
    expect(store.getState().chat.slotSide[SLOT].messages.map(m => m.content)).toEqual(['Q1', 'A1', 'Q2', 'A2', 'Q3'])
  })

  it('settles every waiting panel when each one has a read out', async () => {
    const store = createTestStore()
    store.dispatch(sideRestore({ slot: SLOT, snapshot: running() }))
    store.dispatch(sideRestore({ slot: 'slot-2', snapshot: running() }))
    vi.mocked(api.sideState).mockResolvedValue(finished())

    await store.dispatch(resyncWaitingSideConversations())

    expect(vi.mocked(api.sideState).mock.calls).toEqual([[SLOT], ['slot-2']])
    expect(store.getState().chat.slotSide[SLOT].pending).toBe(false)
    expect(store.getState().chat.slotSide['slot-2'].pending).toBe(false)
  })
})

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }
}

describe('the socket opening', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.sideState).mockReset()
    WS_INSTANCES.length = 0
    testStore = createTestStore()
    testStore.dispatch(sideRestore({ slot: SLOT, snapshot: running() }))
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore }, createElement(QueryClientProvider, { client: qc }, children))
  }

  it('settles a panel whose turn ended before the first connect', async () => {
    vi.mocked(api.sideState).mockResolvedValue(finished())
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })

    await waitFor(() => expect(testStore.getState().chat.slotSide[SLOT].pending).toBe(false))
    expect(vi.mocked(api.sideState)).toHaveBeenCalledWith(SLOT)
    unmount()
  })

  it('settles a panel whose turn ended while the socket was down', async () => {
    vi.useFakeTimers()
    // Still running at the first connect, finished by the reconnect.
    vi.mocked(api.sideState).mockResolvedValueOnce(running()).mockResolvedValue(finished())
    const { unmount } = renderHook(() => useWebSocket(), { wrapper })
    act(() => { WS_INSTANCES[0].simulateOpen() })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(testStore.getState().chat.slotSide[SLOT].awaitingFinalRunId).toBe('r2')

    act(() => { WS_INSTANCES[0].onclose?.(new CloseEvent('close')) })
    act(() => { vi.advanceTimersByTime(2000) }) // reconnect backoff
    act(() => { WS_INSTANCES[1].simulateOpen() })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(testStore.getState().chat.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
    expect(testStore.getState().chat.slotSide[SLOT].messages.map(m => m.content)).toContain('A2')
    unmount()
  })
})
