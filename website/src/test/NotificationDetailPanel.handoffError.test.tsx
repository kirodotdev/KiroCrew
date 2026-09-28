/**
 * The notification panel's two "open in chat" hand-offs must SAY when they fail
 * (#12557). Each swallowed two failures into `console.error`: a rejected
 * request, and a 200 with no `slot` that fell through `if (res.slot)` with no
 * `else` at all. Both left the panel looking exactly like a success.
 *
 * The notice may only ever appear where `/to-chat` did NOT succeed. It says "Try
 * again", and that endpoint is not idempotent: it mints a fresh task-review slot
 * and spawns another agent told to edit the run's work_dir directly, so inviting
 * a repeat of a call that already succeeded would put two agents in one worktree.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { api } from '../api/client'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: { taskRunToChat: vi.fn(), cronToChat: vi.fn(), ackNotification: vi.fn().mockResolvedValue({}) },
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
// The cron notification renders CronAckBar, which polls its own endpoints.
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

/** Renders "Continue in Chat". */
const taskNote: Notification = {
  kind: 'taskrunner', ts: '2026-09-22T07:00:00Z', title: 'Task completed',
  body: '15/15 tasks passed', task_id: 'run-12557', acked: true,
}
/** A second one, for the panel-reuse case. */
const otherNote: Notification = { ...taskNote, ts: '2026-09-22T07:30:00Z', task_id: 'run-other' }
/** No slot of its own, so it renders "View last result". */
const cronNote: Notification = {
  kind: 'cron', ts: '2026-09-22T07:01:00Z', title: 'Nightly sweep finished',
  body: 'exit 0', job_id: 'job-12557', acked: true,
}

/** The one affordance both paths use, whichever is mounted. */
const notice = () => screen.findByTestId('notif-handoff-error')
const click = (name: RegExp) => userEvent.click(screen.getByRole('button', { name }))

beforeEach(() => {
  vi.mocked(api.taskRunToChat).mockReset()
  vi.mocked(api.cronToChat).mockReset()
})

describe('NotificationDetailPanel — "Continue in Chat" (task runner)', () => {
  it('shows a rejected request, and shows the server nothing verbatim', async () => {
    vi.mocked(api.taskRunToChat).mockRejectedValue(new Error('to-chat returned 500'))
    renderWithProviders(<NotificationDetailPanel n={taskNote} onClose={() => {}} />)
    await click(/Continue in Chat/i)
    expect(await notice()).toHaveTextContent('Could not open the chat. Click Continue in Chat to retry.')
    // The server's words name a mechanism, not anything to do about it: they
    // belong in the console, and a reader heard them as machine talk shown by
    // accident.
    expect(await notice()).not.toHaveTextContent('to-chat returned 500')
  })

  it('shows a 200 that carries no slot', async () => {
    vi.mocked(api.taskRunToChat).mockResolvedValue({ ok: true })
    renderWithProviders(<NotificationDetailPanel n={taskNote} onClose={() => {}} />)
    await click(/Continue in Chat/i)
    await waitFor(async () => expect(await notice()).toBeVisible())
    expect((await notice()).textContent?.trim()).not.toBe('')
  })

  it('does not carry the failure onto the next notification', async () => {
    // The bell popover mounts this panel with NO `key` (App.tsx), unlike the
    // full page (`key={selected.ts}`), so a different `n` reuses the instance.
    vi.mocked(api.taskRunToChat).mockRejectedValue(new Error('to-chat returned 500'))
    const { rerender } = renderWithProviders(<NotificationDetailPanel n={taskNote} onClose={() => {}} />)
    await click(/Continue in Chat/i)
    expect(await notice()).toHaveTextContent('Could not open the chat. Click Continue in Chat to retry.')

    rerender(<NotificationDetailPanel n={otherNote} onClose={() => {}} />)
    expect(screen.queryByTestId('notif-handoff-error')).toBeNull()
  })
})

describe('NotificationDetailPanel — "View last result" (cron)', () => {
  it('shows a rejected request', async () => {
    vi.mocked(api.cronToChat).mockRejectedValue(new Error('cron to-chat unreachable'))
    renderWithProviders(<NotificationDetailPanel n={cronNote} onClose={() => {}} />)
    await click(/View Last Result/i)
    // The cron button is called "View last result", so the sentence names that
    // and not a chat the reader can see no control for.
    expect(await notice()).toHaveTextContent('Could not open the result in chat. Click View last result to retry.')
    expect(await notice()).not.toHaveTextContent('cron to-chat unreachable')
  })

  it('shows a 200 that carries no slot', async () => {
    vi.mocked(api.cronToChat).mockResolvedValue({ ok: true })
    renderWithProviders(<NotificationDetailPanel n={cronNote} onClose={() => {}} />)
    await click(/View Last Result/i)
    await waitFor(async () => expect(await notice()).toBeVisible())
    expect((await notice()).textContent?.trim()).not.toBe('')
  })
})
