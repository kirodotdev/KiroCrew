/**
 * The notification panel's server hand-offs ("Continue in Chat" for a task run,
 * "View last result" for a cron job) keep the chat they open through a full
 * `slots` frame that omits it.
 *
 * `/to-chat` mints (or re-mints) the session on the server and answers with its
 * key alone. A full frame the gateway serialized before that, or one held in
 * flight, can reach the tab after the switch. Applied as a whole-list replace it
 * drops the key, and the chat page then clears the active session and returns
 * the user to the chat they came from. The dashboard reducer keeps a key through
 * such a frame only when the add was stamped as a membership add, so this runs
 * the panel against the REAL reducers: a mocked slice would hide whether the
 * hand-off reaches that stamp.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { createTestStore, renderWithProviders } from './helpers'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { api } from '../api/client'
import { sseSlots } from '../store/dashboardSlice'
import { switchSlot } from '../store/chatSlice'
import type { ChatSlot, Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    taskRunToChat: vi.fn(),
    cronToChat: vi.fn(),
    chatSlotDetail: vi.fn(),
    ackNotification: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
// The cron notification renders CronAckBar, which polls its own endpoints.
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const taskNote: Notification = {
  kind: 'taskrunner', ts: '2026-10-22T07:00:00Z', title: 'Task cancelled',
  body: '0/2 tasks passed', task_id: 'run-1', acked: true,
}
/** No slot of its own, so it renders "View last result". */
const cronNote: Notification = {
  kind: 'cron', ts: '2026-10-22T07:01:00Z', title: 'Nightly sweep failed',
  body: 'exit 1', job_id: 'job-1', acked: true,
}

const row = (key: string): ChatSlot => ({
  key, title: key, messages: 2, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})
const wire = (...keys: string[]): ChatSlot[] => keys.map(row)

/** The user is in `origin` when they open the notification, as in the app. */
async function setup(n: Notification) {
  const store = createTestStore()
  store.dispatch(sseSlots(wire('origin', 'other')))
  await store.dispatch(switchSlot('origin'))
  expect(store.getState().chat.activeSlot).toBe('origin')
  renderWithProviders(<NotificationDetailPanel n={n} onClose={() => {}} />, { store })
  // ThemeProvider settles through promise callbacks after mount; drain them
  // before the test acts, so its update lands inside act().
  await settle()
  return store
}

// Drains pending promise callbacks and the React updates they schedule, with
// no wall-clock wait. Every mock here settles through microtasks.
async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 10; i += 1) await Promise.resolve()
  })
}
const keys = (store: Awaited<ReturnType<typeof setup>>) => store.getState().dashboard.slots.map(s => s.key)

beforeEach(() => {
  vi.mocked(api.taskRunToChat).mockReset().mockResolvedValue({ ok: true, slot: 'task-review-1', task_id: 'run-1' })
  vi.mocked(api.cronToChat).mockReset().mockResolvedValue({ ok: true, slot: 'cron-job-1' })
  vi.mocked(api.chatSlotDetail).mockReset().mockResolvedValue({ messages: [], total: 0 })
})

describe('NotificationDetailPanel to-chat hand-offs and a stale slots frame', () => {
  it.each([
    ['Continue in Chat', taskNote, /Continue in Chat/i, 'task-review-1'],
    ['View last result', cronNote, /View Last Result/i, 'cron-job-1'],
  ])('"%s" keeps the new chat through a frame that omits it', async (_label, note, name, key) => {
    const store = await setup(note)
    await userEvent.click(screen.getByRole('button', { name }))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(key))
    expect(keys(store)).toContain(key)

    store.dispatch(sseSlots(wire('origin', 'other')))

    expect(keys(store)).toContain(key)
    expect(store.getState().chat.activeSlot).toBe(key)
  })

  it('keeps the selection on the new chat when its first detail read 404s', async () => {
    // The server listed the key before answering, so a 404 here is a
    // create/fetch race on a slot that exists: the switch must not unwind to
    // the chat the user came from.
    const store = await setup(taskNote)
    vi.mocked(api.chatSlotDetail).mockRejectedValue(Object.assign(new Error('404 not found'), { status: 404 }))
    await userEvent.click(screen.getByRole('button', { name: /Continue in Chat/i }))
    await waitFor(() => expect(vi.mocked(api.chatSlotDetail).mock.calls.map(c => c[0])).toContain('task-review-1'))
    // Settled: the rejected reducer has released the switch's claim.
    await waitFor(() => expect(store.getState().chat.slotSwitchRequestId).toBeNull())
    expect(store.getState().chat.activeSlot).toBe('task-review-1')
    expect(keys(store)).toContain('task-review-1')
  })
})
