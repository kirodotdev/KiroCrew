/** A gateway restart that kills a streaming turn must leave Resume offered.
 *
 *  The tab that watched the reply stream never receives that turn's `_done`:
 *  the process that would send it is gone. On reconnect the catch-up
 *  `refreshSlot` brings back the partial reply plus the restore's
 *  `gateway_restart_interruption` error row and `running: false`. That refresh
 *  used to settle `slotRunning` but leave `slotState` at `'streaming'`, so
 *  `selectComposerBusy` stayed true, the composer kept its Stop button, and
 *  Resume never rendered (reproduced against a real gateway restart; a page
 *  reload was unaffected because a fresh store starts idle).
 *
 *  These tests drive the real reducer and the real composer selectors, so they
 *  pin the user-visible decision rather than one field.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

type Row = { role: string; content: string; cls: string; ts: string; meta?: Record<string, unknown> }

const ts = (s: number) => new Date(Date.UTC(2026, 9, 8, 16, 29, s)).toISOString()

let HISTORY: Row[] = []
let RUNNING = false
let DURING_FETCH: (() => void) | null = null

vi.mock('../api/client', () => ({
  api: {
    chatSlotDetail: vi.fn((slot: string) => {
      if (DURING_FETCH) {
        const fire = DURING_FETCH
        DURING_FETCH = null
        fire()
      }
      return Promise.resolve({
        key: slot,
        messages: [...HISTORY],
        has_more: false,
        next_before: 0,
        total: HISTORY.length,
        running: RUNNING,
        queue: [],
      })
    }),
  },
}))

import chatReducer, {
  refreshSlot,
  selectComposerBusy,
  selectContinuable,
  selectTurnInterrupted,
  sseChatMessage,
} from './chatSlice'
import type { RootState } from './index'

const SLOT = 'slot-restart'

const USER: Row = { role: 'user', content: 'Synthetic prompt', cls: 'msg msg-u', ts: ts(1), meta: { mid: 'm-user' } }
const PARTIAL: Row = { role: 'assistant', content: 'partial reply that stopped mid', cls: 'msg msg-a', ts: ts(2), meta: { mid: 'm-asst' } }
const RESTART_ERROR: Row = {
  role: 'error',
  content: 'This turn was interrupted when the app restarted. Review the partial output above, then resume to continue.',
  cls: 'msg msg-err',
  ts: ts(3),
  meta: { mid: 'm-err', kind: 'gateway_restart_interruption' },
}

/** The store as the streaming tab holds it when the socket drops: the reply is
 *  a live `streaming` row and the run state says a turn is in flight. */
function streamingTab() {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: {
      chat: {
        ...base,
        activeSlot: SLOT,
        messages: [USER, { ...PARTIAL, role: 'streaming', meta: undefined }] as never,
        slotRunning: true,
        slotState: 'streaming' as const,
      },
    },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

/** What the composer reads: the chat slice plus an idle, local dashboard slot. */
const root = (store: ReturnType<typeof streamingTab>): RootState => ({
  chat: store.getState().chat,
  dashboard: { slots: [{ key: SLOT, running: false, stopping: false, subagents_running: false, executor: 'local' }] },
} as unknown as RootState)

/** ChatPage's composer gate: Resume renders only on an idle composer whose
 *  slot is continuable and whose transcript shows an interruption. */
const resumeOffered = (s: RootState) =>
  !selectComposerBusy(s, SLOT) && selectContinuable(s) && selectTurnInterrupted(s)

beforeEach(() => {
  HISTORY = [USER, PARTIAL, RESTART_ERROR]
  RUNNING = false
  DURING_FETCH = null
})

describe('refreshSlot after a gateway restart killed the streaming turn', () => {
  it('idles the stream state so the composer offers Resume', async () => {
    const store = streamingTab()
    expect(selectComposerBusy(root(store), SLOT)).toBe(true)

    await store.dispatch(refreshSlot(SLOT) as never)

    const chat = store.getState().chat
    expect(chat.slotRunning).toBe(false)
    expect(chat.slotState).toBe('idle')
    expect(chat.messages.map(m => m.role)).toEqual(['user', 'assistant', 'error'])
    expect(chat.messages.some(m => m.role === 'streaming')).toBe(false)
    expect(resumeOffered(root(store))).toBe(true)
  })

  it('leaves a new turn that started during the fetch streaming', async () => {
    const store = streamingTab()
    // A frame of a newer turn reduced after the refresh was dispatched: the
    // snapshot predates it and must not idle that stream.
    DURING_FETCH = () => { store.dispatch(sseChatMessage({ slot: SLOT, role: 'chunk', content: 'next turn' })) }

    await store.dispatch(refreshSlot(SLOT) as never)

    expect(store.getState().chat.slotState).not.toBe('idle')
    expect(selectComposerBusy(root(store), SLOT)).toBe(true)
  })

  it('keeps a still-running slot busy', async () => {
    RUNNING = true
    HISTORY = [USER, PARTIAL]
    const store = streamingTab()

    await store.dispatch(refreshSlot(SLOT) as never)

    const chat = store.getState().chat
    expect(chat.slotRunning).toBe(true)
    expect(chat.slotState).toBe('streaming')
    expect(resumeOffered(root(store))).toBe(false)
  })

  it('offers Send, not Resume, when the user had pressed Stop', async () => {
    HISTORY = [USER, PARTIAL, { role: 'inject', content: 'Stopped', cls: 'msg', ts: ts(4), meta: { mid: 'm-stop', kind: 'stop_event' } }]
    const store = streamingTab()

    await store.dispatch(refreshSlot(SLOT) as never)

    const s = root(store)
    expect(store.getState().chat.slotState).toBe('idle')
    expect(selectComposerBusy(s, SLOT)).toBe(false)
    expect(selectTurnInterrupted(s)).toBe(false)
  })
})
