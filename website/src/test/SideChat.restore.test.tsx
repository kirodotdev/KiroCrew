import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import chatReducer, { replaceMessages, restoreSideConversation, sideClose, sideOptimisticAppend, sideOptimisticRollback, sideRestore, sseSideQueue, sseSideResult } from '../store/chatSlice'
import type { SideSnapshot } from '../types'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { readSideChatDraft, writeSideChatDraft } from '../chat-core/composer/sideChatDrafts'
import SideChat from '../pages/chat/SideChat'
import { renderWithProviders, createConnectedTestStore, createTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    sideState: vi.fn(),
    sideTools: vi.fn().mockResolvedValue({ read_only_tools: true, claude_adapter_outdated: false, config_unavailable: false }),
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r9', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
  },
}))

const SLOT = 'slot-1'
const CREATED = '2026-10-21T06:00:00.000Z'

type ChatState = ReturnType<typeof chatReducer>

function fresh(): ChatState {
  return chatReducer(undefined, { type: '@@INIT' })
}

/** Q0 failed (its notice is not stored), Q1 was answered, Q2 is running. */
function snapshot(over: Partial<SideSnapshot> = {}): SideSnapshot {
  return {
    open: true,
    created_at: CREATED,
    last_run_id: 'r2',
    running: true,
    messages: [
      { id: 'u0', run_id: 'r0', role: 'user', content: 'Q0', ts: '2026-10-21T06:00:01.000Z' },
      { id: 'u1', run_id: 'r1', role: 'user', content: 'Q1', ts: '2026-10-21T06:00:02.000Z' },
      { id: 'a1', run_id: 'r1', role: 'assistant', content: 'A1', ts: '2026-10-21T06:00:03.000Z' },
      { id: 'u2', run_id: 'r2', role: 'user', content: 'Q2', ts: '2026-10-21T06:00:04.000Z' },
    ],
    queue: [{ id: 'q3', content: 'Q3', ts: '2026-10-21T06:00:05.000Z' }],
    ...over,
  }
}

const frame = (p: Partial<Parameters<typeof sseSideResult>[0]> & { run_id: string; role: 'user' | 'assistant'; content: string }) =>
  sseSideResult({ slot: SLOT, ...p })

const contents = (s: ChatState) => s.slotSide[SLOT].messages.map(m => (m.is_error ? `!${m.content}` : m.content))

describe('sideRestore', () => {
  it('rebuilds a reloaded panel from the gateway copy, with the running turn waiting for its final frame', () => {
    const s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: snapshot() }))
    const side = s.slotSide[SLOT]
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2'])
    expect(side.messages.map(m => m.id)).toEqual(['u0', 'u1', 'a1', 'u2'])
    expect(side.queue).toEqual([{ id: 'q3', content: 'Q3', ts: '2026-10-21T06:00:05.000Z' }])
    // Restored queue text is the redacted copy, so it is never vouched for as raw.
    expect(side.queue?.[0].raw).toBeUndefined()
    expect(side.lastRunId).toBe('r2')
    expect(side.pending).toBe(true)
    expect(side.streaming).toBe(false)
    expect(side.awaitingFinalRunId).toBe('r2')
    expect(s.slotSideRestored[SLOT]).toBe(true)
  })

  it('holds a joined run back until its final frame, then shows the whole answer', () => {
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: snapshot() }))
    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'tail of the ans' }))
    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'wer' }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].pending).toBe(true)

    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'the whole answer', final: true, message_id: 'a2' }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2', 'the whole answer'])
    expect(s.slotSide[SLOT].messages.at(-1)?.id).toBe('a2')
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(false)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('keeps the complete final answer when frames beat the restore response after reload', () => {
    let s = chatReducer(fresh(), frame({ run_id: 'r2', role: 'assistant', content: 'tail' }))
    s = chatReducer(s, frame({
      run_id: 'r2', role: 'assistant', content: 'head tail', final: true, message_id: 'a2',
    }))
    s = chatReducer(s, sideRestore({
      slot: SLOT,
      snapshot: snapshot({
        running: false,
        queue: [],
        messages: [
          { id: 'u2', run_id: 'r2', role: 'user', content: 'Q2', ts: '2026-10-21T06:00:04.000Z' },
          { id: 'a2', run_id: 'r2', role: 'assistant', content: 'head tail', ts: '2026-10-21T06:00:05.000Z' },
        ],
      }),
    }))

    expect(contents(s)).toEqual(['Q2', 'head tail'])
    expect(s.slotSide[SLOT].messages.filter(m => m.content === 'head tail')).toHaveLength(1)
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(false)
  })

  it('rolls back only the busy state owned by an optimistic bubble', () => {
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: snapshot({ queue: [] }) }))
    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'queued while restoring', ts: '2026-10-21T06:10:00.000Z' },
    }))

    s = chatReducer(s, sideOptimisticRollback(SLOT))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].pending).toBe(true)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBe('r2')

    s = chatReducer(s, frame({
      run_id: 'r2', role: 'assistant', content: 'the whole answer', final: true, message_id: 'a2',
    }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2', 'the whole answer'])
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('keeps a joined run held back after a later turn begins, and lets a rollback settle the panel', () => {
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: snapshot({ queue: [] }) }))
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBe('r2')

    // r2's final frame never reaches this tab, and the gateway starts r3. A late
    // r2 delta is still part-way text, so it stays out of the transcript.
    s = chatReducer(s, frame({ run_id: 'r3', role: 'user', content: 'Q3', message_id: 'u3' }))
    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'tail of r2' }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2', 'Q3'])
    s = chatReducer(s, frame({ run_id: 'r3', role: 'assistant', content: 'A3', final: true, message_id: 'a3' }))
    expect(s.slotSide[SLOT].pending).toBe(false)

    s = chatReducer(s, sideOptimisticAppend({
      slot: SLOT,
      message: { role: 'user', content: 'refused one', ts: '2026-10-21T06:20:00.000Z' },
    }))
    s = chatReducer(s, sideOptimisticRollback(SLOT))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2', 'Q3', 'A3'])
    expect(s.slotSide[SLOT].pending).toBe(false)
  })

  it('draws an interrupted partial answer and notice once for joined and streaming tabs', () => {
    let joined = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: snapshot({ queue: [] }) }))
    joined = chatReducer(joined, frame({
      run_id: 'r2', role: 'assistant', content: 'partial answer', final: true, message_id: 'a2',
    }))
    joined = chatReducer(joined, frame({
      run_id: 'r2', role: 'assistant', content: 'interrupted', is_error: true, final: true, message_id: 'n2',
    }))
    expect(contents(joined)).toEqual(['Q0', 'Q1', 'A1', 'Q2', 'partial answer', '!interrupted'])
    expect(joined.slotSide[SLOT].messages.filter(m => m.id === 'a2')).toHaveLength(1)
    expect(joined.slotSide[SLOT].messages.filter(m => m.id === 'n2')).toHaveLength(1)
    expect(joined.slotSide[SLOT].pending).toBe(false)
    expect(joined.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()

    let streaming = chatReducer(fresh(), frame({
      run_id: 'r2', role: 'user', content: 'Q2', message_id: 'u2',
    }))
    streaming = chatReducer(streaming, frame({
      run_id: 'r2', role: 'assistant', content: 'partial ',
    }))
    streaming = chatReducer(streaming, frame({
      run_id: 'r2', role: 'assistant', content: 'partial answer', final: true, message_id: 'a2',
    }))
    streaming = chatReducer(streaming, frame({
      run_id: 'r2', role: 'assistant', content: 'interrupted', is_error: true, final: true, message_id: 'n2',
    }))
    expect(contents(streaming)).toEqual(['Q2', 'partial answer', '!interrupted'])
    expect(streaming.slotSide[SLOT].messages.filter(m => m.id === 'a2')).toHaveLength(1)
    expect(streaming.slotSide[SLOT].messages.filter(m => m.id === 'n2')).toHaveLength(1)
    expect(streaming.slotSide[SLOT].pending).toBe(false)
    expect(streaming.slotSide[SLOT].streaming).toBe(false)
  })

  it('draws a frame the read already holds once, and lets it leave the busy state alone', () => {
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: snapshot() }))
    const before = s.slotSide[SLOT]
    s = chatReducer(s, frame({ run_id: 'r1', role: 'user', content: 'Q1', message_id: 'u1' }))
    s = chatReducer(s, frame({ run_id: 'r1', role: 'assistant', content: 'A', }))
    s = chatReducer(s, frame({ run_id: 'r1', role: 'assistant', content: 'A1', final: true, message_id: 'a1' }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].pending).toBe(before.pending)
    expect(s.slotSide[SLOT].lastRunId).toBe('r2')
  })

  it('files a late notice for an earlier turn under that turn without clearing the running one', () => {
    let s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: snapshot() }))
    s = chatReducer(s, frame({ run_id: 'r0', role: 'assistant', content: 'failed', is_error: true, final: true }))
    expect(contents(s)).toEqual(['Q0', '!failed', 'Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].pending).toBe(true)
    expect(s.slotSide[SLOT].lastRunId).toBe('r2')
  })

  it('keeps what frames drew first: an unstored notice by its turn, and a streaming answer this tab saw begin', () => {
    let s = fresh()
    s = chatReducer(s, frame({ run_id: 'r0', role: 'assistant', content: 'failed', is_error: true, final: true }))
    s = chatReducer(s, frame({ run_id: 'r2', role: 'user', content: 'Q2', message_id: 'u2' }))
    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'the be' }))
    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot() }))

    expect(contents(s)).toEqual(['Q0', '!failed', 'Q1', 'A1', 'Q2', 'the be'])
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
    expect(s.slotSide[SLOT].streaming).toBe(true)

    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'the best answer', final: true, message_id: 'a2' }))
    expect(contents(s)).toEqual(['Q0', '!failed', 'Q1', 'A1', 'Q2', 'the best answer'])
  })

  it('drops a streamed answer the gateway already stored, and a tail of a run this tab joined late', () => {
    let s = fresh()
    // Joined r2 mid-stream: no question frame, only a tail.
    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'ail' }))
    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot() }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBe('r2')

    let t = fresh()
    t = chatReducer(t, frame({ run_id: 'r1', role: 'user', content: 'Q1', message_id: 'u1' }))
    t = chatReducer(t, frame({ run_id: 'r1', role: 'assistant', content: 'A' }))
    t = chatReducer(t, sideRestore({ slot: SLOT, snapshot: snapshot({ running: false, last_run_id: 'r1', messages: snapshot().messages?.slice(0, 3) }) }))
    expect(contents(t)).toEqual(['Q0', 'Q1', 'A1'])
    expect(t.slotSide[SLOT].pending).toBe(false)
    expect(t.slotSide[SLOT].streaming).toBe(false)
  })

  it('follows a turn this tab saw start after the read was answered', () => {
    let s = fresh()
    s = chatReducer(s, frame({ run_id: 'r3', role: 'user', content: 'Q3', message_id: 'u3' }))
    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot({ running: false, last_run_id: 'r1', messages: snapshot().messages?.slice(0, 3), queue: [] }) }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q3'])
    expect(s.slotSide[SLOT].lastRunId).toBe('r3')
    expect(s.slotSide[SLOT].pending).toBe(true)
  })

  it('keeps a repeated optimistic question separate from a restored running turn', () => {
    let s = fresh()
    s = chatReducer(s, sideOptimisticAppend({ slot: SLOT, message: { role: 'user', content: 'Q2', ts: '2026-10-21T06:10:00.000Z' } }))
    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot({ queue: [] }) }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2', 'Q2'])
    expect(s.slotSide[SLOT].messages.at(-1)?.optimistic).toBe(true)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBe('r2')
    expect(s.slotSide[SLOT].pending).toBe(true)

    s = chatReducer(s, sideOptimisticRollback(SLOT))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBe('r2')
    expect(s.slotSide[SLOT].pending).toBe(true)

    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'tail' }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2'])
    expect(s.slotSide[SLOT].pending).toBe(true)

    s = chatReducer(s, frame({ run_id: 'r2', role: 'assistant', content: 'head tail', final: true, message_id: 'a2' }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2', 'head tail'])
    expect(s.slotSide[SLOT].messages.filter(m => m.content === 'head tail')).toHaveLength(1)
    expect(s.slotSide[SLOT].pending).toBe(false)
    expect(s.slotSide[SLOT].streaming).toBe(false)
    expect(s.slotSide[SLOT].awaitingFinalRunId).toBeUndefined()
  })

  it('does not take an older question with the same text for a just-sent one', () => {
    let s = fresh()
    s = chatReducer(s, sideOptimisticAppend({ slot: SLOT, message: { role: 'user', content: 'Q1', ts: '2026-10-21T06:10:00.000Z' } }))
    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot({ queue: [] }) }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q2', 'Q1'])
    expect(s.slotSide[SLOT].messages.at(-1)?.optimistic).toBe(true)
  })

  it('keeps a question this tab sent before its frame, and keeps the panel busy for it', () => {
    let s = fresh()
    s = chatReducer(s, sideOptimisticAppend({ slot: SLOT, message: { role: 'user', content: 'Q4', ts: '2026-10-21T06:10:00.000Z' } }))
    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot({ running: false, last_run_id: 'r1', messages: snapshot().messages?.slice(0, 3) }) }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q4'])
    expect(s.slotSide[SLOT].messages.at(-1)?.optimistic).toBe(true)
    expect(s.slotSide[SLOT].pending).toBe(true)

    s = chatReducer(s, frame({ run_id: 'r4', role: 'user', content: 'Q4', message_id: 'u4' }))
    expect(contents(s)).toEqual(['Q0', 'Q1', 'A1', 'Q4'])
    expect(s.slotSide[SLOT].messages.at(-1)?.id).toBe('u4')
  })

  it('keeps this tab’s raw queue text and leaves out entries it already saw leave', () => {
    let s = fresh()
    s = chatReducer(s, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q3', content: 'Q3 raw', raw: true }))
    s = chatReducer(s, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'q4', content: 'Q4' }))
    s = chatReducer(s, sseSideQueue({ slot: SLOT, action: 'drain', queue_id: 'q4' }))
    const withGone = snapshot({ queue: [{ id: 'q3', content: 'Q3', ts: 't' }, { id: 'q4', content: 'Q4', ts: 't' }] })
    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: withGone }))
    expect(s.slotSide[SLOT].queue?.map(e => [e.id, e.content, e.raw])).toEqual([['q3', 'Q3 raw', true]])
  })

  it('does not restore a queued question drained before a fresh tab receives its snapshot', () => {
    let s = chatReducer(fresh(), sseSideQueue({ slot: SLOT, action: 'drain', queue_id: 'q3' }))
    expect(s.slotSide[SLOT]?.queue ?? []).toEqual([])

    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot() }))
    expect(s.slotSide[SLOT].queue?.map(entry => entry.id) ?? []).toEqual([])
  })

  it('does not restore or release a queued question cancelled before a fresh tab receives its snapshot', () => {
    let s = chatReducer(fresh(), sseSideQueue({
      slot: SLOT, action: 'cancel', queue_id: 'q3', content: 'must not be released', raw: true,
    }))
    expect(s.slotSide[SLOT]?.queue ?? []).toEqual([])
    expect(s.slotSide[SLOT]?.releasedText).toBeUndefined()

    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot() }))
    expect(s.slotSide[SLOT].queue?.map(entry => entry.id) ?? []).toEqual([])
    expect(s.slotSide[SLOT].releasedText).toBeUndefined()
  })

  it('retires unseen terminal queue frames when the tab already has side state', () => {
    let s = chatReducer(fresh(), frame({ run_id: 'r2', role: 'user', content: 'Q2', message_id: 'u2' }))
    s = chatReducer(s, sseSideQueue({ slot: SLOT, action: 'drain', queue_id: 'q3' }))
    s = chatReducer(s, sseSideQueue({
      slot: SLOT, action: 'cancel', queue_id: 'q4', content: 'must not be released', raw: true,
    }))
    expect(s.slotSide[SLOT].releasedText).toBeUndefined()

    s = chatReducer(s, sideRestore({
      slot: SLOT,
      snapshot: snapshot({
        queue: [
          { id: 'q3', content: 'Q3', ts: '2026-10-21T06:00:05.000Z' },
          { id: 'q4', content: 'Q4', ts: '2026-10-21T06:00:06.000Z' },
        ],
      }),
    }))
    expect(s.slotSide[SLOT].queue?.map(entry => entry.id) ?? []).toEqual([])
    expect(s.slotSide[SLOT].releasedText).toBeUndefined()
  })

  it('keeps local head inserts before restored entries and ordinary local pushes after them', () => {
    let s = chatReducer(fresh(), sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-front', content: 'requeued steer', front: true,
    }))
    s = chatReducer(s, sseSideQueue({
      slot: SLOT, action: 'push', queue_id: 'q-tail', content: 'later question',
    }))

    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot() }))
    expect(s.slotSide[SLOT].queue?.map(entry => entry.id)).toEqual(['q-front', 'q3', 'q-tail'])
  })

  it('keeps terminal queue frames inert for closed sides without weakening open-slot retirement', () => {
    let closed = chatReducer(fresh(), sideClose(SLOT))
    closed = chatReducer(closed, sseSideQueue({ slot: SLOT, action: 'drain', queue_id: 'q3' }))
    closed = chatReducer(closed, sseSideQueue({
      slot: SLOT, action: 'cancel', queue_id: 'q4', content: 'must not be released', raw: true,
    }))
    expect(closed.slotSide[SLOT]).toBeUndefined()

    let open = chatReducer(fresh(), sseSideQueue({ slot: SLOT, action: 'drain', queue_id: 'q3' }))
    open = chatReducer(open, sideRestore({ slot: SLOT, snapshot: snapshot() }))
    expect(open.slotSide[SLOT].queue?.map(entry => entry.id) ?? []).toEqual([])
  })

  it('ignores a read that lands after the panel was closed, and any second read', () => {
    let s = chatReducer(fresh(), sideClose(SLOT))
    s = chatReducer(s, sideRestore({ slot: SLOT, snapshot: snapshot() }))
    expect(s.slotSide[SLOT]).toBeUndefined()

    let t = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: { open: false } }))
    t = chatReducer(t, sideRestore({ slot: SLOT, snapshot: snapshot() }))
    expect(t.slotSide[SLOT]).toBeUndefined()
    expect(t.slotSideRestored[SLOT]).toBe(true)
  })

  it('marks a restored conversation to count its banner turns by time', () => {
    const s = chatReducer(fresh(), sideRestore({ slot: SLOT, snapshot: snapshot() }))
    expect(s.slotSide[SLOT].countsTurnsSinceCreated).toBe(true)
    expect(s.slotSide[SLOT].createdAt).toBe(CREATED)
  })
})

describe('SideChat restore on mount', () => {
  beforeEach(() => {
    vi.mocked(api.sideState).mockReset()
    vi.mocked(api.sideTurn).mockClear()
    vi.mocked(api.sideOpen).mockClear()
  })

  const settledSnapshot = () => snapshot({ running: false, last_run_id: 'r1', messages: snapshot().messages?.slice(0, 3), queue: [] })
  /** The footer's tool note is the panel's last asynchronous update on mount. */
  const footerSettled = async () => { expect(await screen.findByRole('note')).toBeInTheDocument() }
  const flush = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

  it('reads the gateway copy once and draws it', async () => {
    vi.mocked(api.sideState).mockResolvedValue(settledSnapshot())
    const store = createConnectedTestStore()
    const first = renderWithProviders(<SideChat slot={SLOT} />, { store })
    expect(await screen.findByText('A1')).toBeInTheDocument()
    expect(screen.getByText('Q0')).toBeInTheDocument()
    await footerSettled()
    expect(api.sideState).toHaveBeenCalledTimes(1)
    expect(api.sideState).toHaveBeenCalledWith(SLOT)

    first.unmount()
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    expect(await screen.findByText('A1')).toBeInTheDocument()
    await footerSettled()
    expect(api.sideState).toHaveBeenCalledTimes(1)
  })

  it('counts the banner turns from the parent turns newer than the conversation, as they load', async () => {
    vi.mocked(api.sideState).mockResolvedValue(settledSnapshot())
    const store = createConnectedTestStore({ chat: { ...fresh(), activeSlot: SLOT, messages: [] } } as never)
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    expect(await screen.findByText('A1')).toBeInTheDocument()
    await footerSettled()
    expect(screen.getByText(/0 turns/)).toBeInTheDocument()

    // The parent transcript lands after the restore: two turns before the side
    // conversation opened, two after it, and a tool row that is not a turn.
    const at = (ts: string, role = 'user') => ({ role, content: 'm', cls: '', ts })
    act(() => {
      store.dispatch(replaceMessages([
        at('2026-10-21T05:00:00.000Z'), at('2026-10-21T05:01:00.000Z', 'assistant'),
        at('2026-10-21T06:30:00.000Z'), at('2026-10-21T06:31:00.000Z', 'assistant'),
        at('2026-10-21T06:32:00.000Z', 'tool'),
      ]))
    })
    expect(screen.getByText(/2 turns/)).toBeInTheDocument()
  })

  it('holds questions back while the read has failed, and lets them through once Retry loads it', async () => {
    vi.mocked(api.sideState).mockRejectedValue(new Error('offline'))
    const store = createConnectedTestStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
    const notice = await screen.findByTestId('side-chat-restore-failed')
    await footerSettled()
    expect(notice).toHaveTextContent("Couldn't load this side conversation")
    expect(store.getState().chat.slotSideRestored[SLOT]).toBeUndefined()

    act(() => { writeSideChatDraft(SLOT, 'what did I ask?') })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    // The send tries the read again first; it fails again, so nothing is asked
    // and the text comes back.
    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('what did I ask?'))
    await flush()
    expect(api.sideOpen).not.toHaveBeenCalled()
    expect(api.sideTurn).not.toHaveBeenCalled()

    vi.mocked(api.sideState).mockResolvedValue(settledSnapshot())
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('A1')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('side-chat-restore-failed')).not.toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'what did I ask?', undefined))
    await flush()
  })

  it('makes a question sent while the read is in flight wait for it, and hands it back when the read fails', async () => {
    let reject: (e: unknown) => void = () => {}
    vi.mocked(api.sideState).mockReturnValue(new Promise<SideSnapshot>((_resolve, fail) => { reject = fail }))
    const store = createConnectedTestStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
    await footerSettled()

    act(() => { writeSideChatDraft(SLOT, 'asked too early') })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await flush()
    expect(api.sideOpen).not.toHaveBeenCalled()
    expect(api.sideTurn).not.toHaveBeenCalled()

    await act(async () => { reject(new Error('offline')) })
    expect(await screen.findByTestId('side-chat-restore-failed')).toBeInTheDocument()
    await waitFor(() => expect(readSideChatDraft(SLOT)).toBe('asked too early'))
    expect(api.sideOpen).not.toHaveBeenCalled()
    expect(api.sideTurn).not.toHaveBeenCalled()
    expect(store.getState().chat.slotSide[SLOT]?.messages ?? []).toEqual([])
    act(() => { writeSideChatDraft(SLOT, '') })
  })

  it('asks a question sent while the read is in flight once the read lands, after drawing it', async () => {
    let answer: (s: SideSnapshot) => void = () => {}
    vi.mocked(api.sideState).mockReturnValue(new Promise<SideSnapshot>(resolve => { answer = resolve }))
    const store = createConnectedTestStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
    await footerSettled()

    act(() => { writeSideChatDraft(SLOT, 'and now?') })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await flush()
    expect(api.sideTurn).not.toHaveBeenCalled()

    await act(async () => { answer(settledSnapshot()) })
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'and now?', undefined))
    expect(screen.getByText('A1')).toBeInTheDocument()
    await flush()
  })

  it('keeps a restored running turn busy when an early send is queued', async () => {
    let answer: (s: SideSnapshot) => void = () => {}
    vi.mocked(api.sideState).mockReturnValue(new Promise<SideSnapshot>(resolve => { answer = resolve }))
    vi.mocked(api.sideTurn).mockResolvedValueOnce({
      ok: true, queued: true, queue_id: 'q1', still_queued: true,
    })
    const store = createConnectedTestStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
    await footerSettled()

    act(() => { writeSideChatDraft(SLOT, 'queued while restoring') })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await flush()
    expect(api.sideTurn).not.toHaveBeenCalled()

    await act(async () => {
      answer(snapshot({
        messages: [{ id: 'u2', run_id: 'r2', role: 'user', content: 'running question', ts: '2026-10-21T06:00:04.000Z' }],
        queue: [],
      }))
    })
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'queued while restoring', undefined))
    expect(await screen.findByText('queued while restoring')).toBeInTheDocument()
    expect(screen.getByText('Thinking…')).toBeInTheDocument()
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
    expect(store.getState().chat.slotSide[SLOT].awaitingFinalRunId).toBe('r2')

    act(() => {
      store.dispatch(sseSideResult({
        slot: SLOT, run_id: 'r2', role: 'assistant', content: 'restored answer', final: true, message_id: 'a2',
      }))
    })
    expect(await screen.findByText('restored answer')).toBeInTheDocument()
    expect(screen.queryByText('Thinking…')).not.toBeInTheDocument()
    expect(screen.queryByTestId('stop-button-armed')).not.toBeInTheDocument()
    expect(store.getState().chat.slotSide[SLOT].pending).toBe(false)
  })

  it('keeps a repeated early send separate from a restored running turn', async () => {
    let answer: (s: SideSnapshot) => void = () => {}
    let acceptQueued: (value: Awaited<ReturnType<typeof api.sideTurn>>) => void = () => {}
    vi.mocked(api.sideState).mockReturnValue(new Promise<SideSnapshot>(resolve => { answer = resolve }))
    vi.mocked(api.sideTurn).mockReturnValueOnce(new Promise(resolve => { acceptQueued = resolve }))
    const store = createConnectedTestStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
    await footerSettled()

    act(() => { writeSideChatDraft(SLOT, 'running question') })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await flush()
    expect(api.sideTurn).not.toHaveBeenCalled()

    await act(async () => {
      answer(snapshot({
        messages: [{ id: 'u2', run_id: 'r2', role: 'user', content: 'running question', ts: '2026-10-21T06:00:04.000Z' }],
        queue: [],
      }))
    })
    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'running question', undefined))
    expect(await screen.findAllByText('running question')).toHaveLength(2)

    await act(async () => {
      acceptQueued({ ok: true, queued: true, queue_id: 'q1', still_queued: true })
    })
    await waitFor(() => expect(store.getState().chat.slotSide[SLOT].queue?.map(entry => entry.content)).toEqual(['running question']))
    expect(screen.getAllByText('running question')).toHaveLength(2)
    expect(store.getState().chat.slotSide[SLOT].messages.filter(message => message.content === 'running question')).toHaveLength(1)
    expect(screen.getByText('Thinking…')).toBeInTheDocument()
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()

    act(() => {
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'tail' }))
    })
    expect(screen.queryByText('tail')).not.toBeInTheDocument()

    act(() => {
      store.dispatch(sseSideResult({
        slot: SLOT, run_id: 'r2', role: 'assistant', content: 'head tail', final: true, message_id: 'a2',
      }))
    })
    expect(await screen.findByText('head tail')).toBeInTheDocument()
    expect(screen.getAllByText('head tail')).toHaveLength(1)
    expect(screen.queryByText('tailhead tail')).not.toBeInTheDocument()
    expect(screen.queryByText('Thinking…')).not.toBeInTheDocument()
    expect(screen.queryByTestId('stop-button-armed')).not.toBeInTheDocument()
    expect(store.getState().chat.slotSide[SLOT].pending).toBe(false)
  })

  it('takes a 404 as nothing to restore', async () => {
    vi.mocked(api.sideState).mockRejectedValue(new ApiError(404, 'not found', ''))
    const store = createConnectedTestStore()
    renderWithProviders(<SideChat slot={SLOT} />, { store, queryDefaults: { retry: false } })
    await waitFor(() => expect(store.getState().chat.slotSideRestored[SLOT]).toBe(true))
    await footerSettled()
    expect(screen.queryByTestId('side-chat-restore-failed')).not.toBeInTheDocument()
    expect(store.getState().chat.slotSide[SLOT]).toBeUndefined()
  })
})

describe('restoreSideConversation', () => {
  beforeEach(() => {
    vi.mocked(api.sideState).mockReset()
  })

  it('shares one read between callers and skips a slot already settled', async () => {
    let answer: (s: SideSnapshot) => void = () => {}
    vi.mocked(api.sideState).mockReturnValue(new Promise<SideSnapshot>(resolve => { answer = resolve }))
    const store = createTestStore()
    const first = store.dispatch(restoreSideConversation(SLOT))
    const second = store.dispatch(restoreSideConversation(SLOT))
    answer(snapshot({ running: false }))
    await Promise.all([first.unwrap(), second.unwrap()])
    expect(api.sideState).toHaveBeenCalledTimes(1)
    expect(store.getState().chat.slotSide[SLOT].messages).toHaveLength(4)

    await store.dispatch(restoreSideConversation(SLOT)).unwrap()
    expect(api.sideState).toHaveBeenCalledTimes(1)
  })

  it('rejects on a failed read and settles nothing, so the caller can hold its question', async () => {
    vi.mocked(api.sideState).mockRejectedValue(new Error('offline'))
    const store = createTestStore()
    await expect(store.dispatch(restoreSideConversation(SLOT)).unwrap()).rejects.toThrow('offline')
    expect(store.getState().chat.slotSideRestored[SLOT]).toBeUndefined()
  })
})
