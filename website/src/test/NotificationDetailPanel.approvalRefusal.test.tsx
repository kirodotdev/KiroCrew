/**
 * The detail panel's Approve/Reject read the same store marks as the feed.
 * A terminal refusal (the approval expired or was already decided elsewhere)
 * or a decision that lands RETIRES the row: both buttons are withdrawn and a
 * neutral notice with Dismiss replaces them. The row, and so the panel, leaves
 * only when a DELETE succeeds; a failed DELETE keeps both.
 * Any other decision failure keeps the buttons, because the approval may
 * still be pending.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders, createTestStore } from './helpers'
import type { RootState } from '../store'
import { retireApprovalNote } from '../store/notificationsSlice'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { refusedNotice } from '../components/notifications/notifMeta'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { ApiError } from '../api/apiError'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: { resolveApproval: vi.fn(), ackNotification: vi.fn().mockResolvedValue({}), unackNotification: vi.fn().mockResolvedValue({}), deleteNotification: vi.fn().mockResolvedValue({}) },
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const approvalNote: Notification = {
  kind: 'approval', ts: '2026-09-28T13:55:22Z', title: 'Tool approval: shell',
  body: 'gh issue view 14704', approval_id: 'apr-gone', acked: true,
}
const notFound = () => Object.assign(new Error('not found or expired'), { status: 404 })

function renderPanel(onClose = vi.fn()) {
  const store = createTestStore({ notifications: { items: [approvalNote] } as RootState['notifications'] })
  const view = renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={onClose} />, { store })
  return { ...view, store, onClose }
}

beforeEach(() => {
  vi.mocked(api.resolveApproval).mockReset()
  vi.mocked(api.deleteNotification).mockReset()
  vi.mocked(api.deleteNotification).mockResolvedValue({})
})

describe('NotificationDetailPanel approval decisions', () => {
  it('a decide on a slotless coordinator row is bound to its instance', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue({})
    const n: Notification = { ...approvalNote, approval_instance: 'inst-cron' }
    const store = createTestStore({ notifications: { items: [n] } as RootState['notifications'] })
    renderWithProviders(<NotificationDetailPanel n={n} onClose={vi.fn()} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(api.resolveApproval).toHaveBeenCalledWith('apr-gone', 'reject', { origin: 'coordinator', slot: '', instance: 'inst-cron' })
  })
  it('a 404 retires the row: buttons withdrawn, error notice, no DELETE, panel stays open', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(notFound())
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(refusedNotice())
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Reject/ })).toBeNull()
    expect(store.getState().notifications.retiredApprovals).toEqual({ [approvalNote.ts]: 'refused' })
    // The decide failed, so the sentence is an error notice, not muted status.
    expect(screen.getByTestId('notif-approval-retired')).toHaveAttribute('role', 'alert')
    expect(api.deleteNotification).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('a 404 moves keyboard focus to the notice that replaced the pressed button', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(notFound())
    renderPanel()
    screen.getByRole('button', { name: /Approve/ }).focus()
    await userEvent.keyboard('{Enter}')
    await screen.findByTestId('notif-approval-retired')
    await waitFor(() => { expect(document.activeElement).toBe(screen.getByTestId('notif-approval-refusal-focus')) })
  })

  it('a 404 does not pull focus away from a reader who has moved on', async () => {
    let refuse: (e: unknown) => void = () => {}
    vi.mocked(api.resolveApproval).mockImplementationOnce(() => new Promise((_, rej) => { refuse = rej }))
    renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    const markUnread = screen.getByRole('button', { name: /Mark unread/i })
    markUnread.focus()
    await act(async () => { refuse(notFound()) })
    await screen.findByTestId('notif-approval-retired')
    expect(document.activeElement).toBe(markUnread)
    await userEvent.click(markUnread)
  })

  it('Dismiss on a retired row removes it once the server confirms, then closes', async () => {
    const { store, onClose } = renderPanel()
    act(() => { store.dispatch(retireApprovalNote({ ts: approvalNote.ts, why: 'gone' })) })
    await userEvent.click(screen.getByTestId('notif-decided-dismiss'))
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
    expect(store.getState().notifications.items).toEqual([])
  })

  it('a decision that lands but whose DELETE fails keeps the row and the panel, with the outcome', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue({})
    vi.mocked(api.deleteNotification).mockRejectedValueOnce(new Error('network down'))
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.rejected'))
    await waitFor(() => { expect(screen.getByTestId('notif-decided-dismiss')).toBeEnabled() })
    expect(store.getState().notifications.items).toHaveLength(1)
    expect(onClose).not.toHaveBeenCalled()
    // The failure is said, under the Dismiss that retries it.
    const failed = screen.getByTestId('notif-dismiss-failed')
    expect(failed).toHaveAttribute('role', 'alert')
    expect(failed).toHaveTextContent(i18nT('components.notifications.notificationFeed.could_not_dismiss_try_again'))
    // The panel's own Dismiss retries the removal.
    await userEvent.click(screen.getByTestId('notif-decided-dismiss'))
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
    expect(store.getState().notifications.items).toEqual([])
  })

  it('a Dismiss whose DELETE fails keeps the panel open and says so', async () => {
    vi.mocked(api.deleteNotification).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { store, onClose } = renderPanel()
    act(() => { store.dispatch(retireApprovalNote({ ts: approvalNote.ts, why: 'gone' })) })
    expect(screen.queryByTestId('notif-dismiss-failed')).toBeNull()
    await userEvent.click(screen.getByTestId('notif-decided-dismiss'))
    expect(await screen.findByTestId('notif-dismiss-failed')).toHaveTextContent(
      i18nT('components.notifications.notificationFeed.could_not_dismiss_try_again'),
    )
    expect(onClose).not.toHaveBeenCalled()
    expect(store.getState().notifications.items).toHaveLength(1)
  })

  it('a double Dismiss while the DELETE is in flight sends one request', async () => {
    let settle: (v: unknown) => void = () => {}
    vi.mocked(api.deleteNotification).mockImplementationOnce(() => new Promise(res => { settle = res }))
    const { store, onClose } = renderPanel()
    act(() => { store.dispatch(retireApprovalNote({ ts: approvalNote.ts, why: 'approve' })) })
    const dismiss = screen.getByTestId('notif-decided-dismiss')
    await userEvent.click(dismiss)
    await userEvent.click(dismiss)
    expect(api.deleteNotification).toHaveBeenCalledTimes(1)
    expect(dismiss).toBeDisabled()
    await act(async () => { settle({ ok: true }) })
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
  })

  it('a remounted panel over a retired row still withdraws the buttons', () => {
    const store = createTestStore({ notifications: {
      items: [approvalNote], retiredApprovals: { [approvalNote.ts]: 'gone' },
    } as RootState['notifications'] })
    renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={() => {}} />, { store })
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
    expect(screen.getByTestId('notif-decided-dismiss')).toBeTruthy()
  })

  it('Approve/Reject are disabled while the decision is in flight', async () => {
    let settle: (v: unknown) => void = () => {}
    vi.mocked(api.resolveApproval).mockImplementationOnce(() => new Promise(res => { settle = res }))
    const { onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    const approve = screen.getByRole('button', { name: /Approve/ })
    expect(approve).toBeDisabled()
    expect(approve).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('button', { name: /Reject/ })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    await act(async () => { settle({}) })
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
    expect(api.resolveApproval).toHaveBeenCalledTimes(1)
  })

  it('a non-terminal failure keeps both buttons and quotes the server reason', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(new ApiError(403, 'approval belongs to another session'))
    const { store } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(await screen.findByTestId('notif-approval-refusal')).toHaveTextContent(
      i18nT('components.approvalCard.decision_not_recorded_error', { error: 'approval belongs to another session' }),
    )
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reject/ })).toBeTruthy()
    expect(store.getState().notifications.retiredApprovals ?? {}).toEqual({})
  })

  it('a transport failure with no response keeps the buttons and says the decision may not have landed', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(new TypeError('Failed to fetch'))
    renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(await screen.findByTestId('notif-approval-refusal')).toHaveTextContent(
      i18nT('components.approvalCard.decision_failed'),
    )
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
  })
})
