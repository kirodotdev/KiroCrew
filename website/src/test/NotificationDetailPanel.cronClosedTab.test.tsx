/**
 * "View last result" on a cron notification after the user closed the job's
 * chat tab (C-CRONTAB).
 *
 * The close reached this tab as a durable `removed` patch, so `cron-<id>` is
 * not in the slot list when `/to-chat` re-mints it and answers with the key.
 * The chat page drops an active session its slot list lacks
 * (`useChatPageSessionController`, the "different mode" effect), and the
 * `slots` frame that lists the re-minted tab trails the HTTP answer by a
 * coalescing window. So the key must be listed by the time the switch makes
 * it active, or the page clears the selection and reopens the previous chat.
 * Runs against the real reducers.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { createTestStore, renderWithProviders } from './helpers'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { api } from '../api/client'
import { sseSlots, sseSlotPatch } from '../store/dashboardSlice'
import { switchSlot } from '../store/chatSlice'
import type { ChatSlot, Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
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

/** A failed run's notification carries no slot, so the panel offers "View last result". */
const cronNote: Notification = {
  kind: 'cron', ts: '2026-10-22T07:01:00Z', title: 'Nightly sweep failed',
  body: 'exit 1', job_id: 'job-1', acked: true,
}
const row = (key: string): ChatSlot => ({
  key, title: key, messages: 2, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})

beforeEach(() => {
  vi.mocked(api.cronToChat).mockReset().mockResolvedValue({ ok: true, slot: 'cron-job-1' })
  vi.mocked(api.chatSlotDetail).mockReset().mockResolvedValue({ messages: [], total: 0 })
})

describe('NotificationDetailPanel "View last result" with the cron tab closed', () => {
  it('lists the re-minted tab before it becomes the active session', async () => {
    const store = createTestStore()
    // The run opened the job's tab, then the user closed it.
    store.dispatch(sseSlots(['origin', 'other', 'cron-job-1'].map(row)))
    store.dispatch(sseSlotPatch({ slots: [], removed: ['cron-job-1'] }))
    await store.dispatch(switchSlot('origin'))
    expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('cron-job-1')

    // Record whether the key was listed on the dispatch that activated it:
    // the chat page's effect reads the two together on that commit.
    let listedWhenActivated: boolean | null = null
    const unsubscribe = store.subscribe(() => {
      const s = store.getState()
      if (listedWhenActivated === null && s.chat.activeSlot === 'cron-job-1') {
        listedWhenActivated = s.dashboard.slots.some(x => x.key === 'cron-job-1')
      }
    })
    renderWithProviders(<NotificationDetailPanel n={cronNote} onClose={() => {}} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /View Last Result/i }))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('cron-job-1'))
    unsubscribe()

    expect(listedWhenActivated).toBe(true)
    // The re-mint is live again, so the close's durable evidence is gone too.
    expect(store.getState().dashboard.durablyRemoved['cron-job-1']).toBeUndefined()
  })
})
