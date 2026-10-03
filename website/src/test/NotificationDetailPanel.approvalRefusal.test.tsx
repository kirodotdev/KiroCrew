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
  api: { decideApproval: vi.fn(), ackNotification: vi.fn().mockResolvedValue({}), unackNotification: vi.fn().mockResolvedValue({}), deleteNotification: vi.fn().mockResolvedValue({}) },
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const approvalNote: Notification = {
  kind: 'approval', ts: '2026-09-28T13:55:22Z', title: 'Tool approval: shell',
  body: 'gh issue view 14704', approval_id: 'apr-gone', approval_instance: 'inst-gone', acked: true,
}
const notFound = () => Object.assign(new Error('not found or expired'), { status: 404 })

function renderPanel(onClose = vi.fn()) {
  const store = createTestStore({ notifications: { items: [approvalNote] } as RootState['notifications'] })
  const view = renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={onClose} />, { store })
  return { ...view, store, onClose }
}

beforeEach(() => {
  vi.mocked(api.decideApproval).mockReset()
  vi.mocked(api.deleteNotification).mockReset()
  vi.mocked(api.deleteNotification).mockResolvedValue({})
})

describe('NotificationDetailPanel approval decisions', () => {
  it('a decide on a slotless coordinator row is bound to its instance', async () => {
    vi.mocked(api.decideApproval).mockResolvedValue({})
    const n: Notification = { ...approvalNote, approval_instance: 'inst-cron' }
    const store = createTestStore({ notifications: { items: [n] } as RootState['notifications'] })
    renderWithProviders(<NotificationDetailPanel n={n} onClose={vi.fn()} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(api.decideApproval).toHaveBeenCalledWith({ origin: 'coordinator', id: 'apr-gone', slot: '', instance: 'inst-cron' }, 'reject')
  })
  it('a row that names no request sends nothing and retires as refused', async () => {
    const { approval_instance: _none, ...n } = approvalNote
    const store = createTestStore({ notifications: { items: [n] } as RootState['notifications'] })
    renderWithProviders(<NotificationDetailPanel n={n} onClose={vi.fn()} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(api.decideApproval).not.toHaveBeenCalled()
    expect(store.getState().notifications.retiredApprovals).toEqual({ [n.ts]: 'refused' })
    expect(screen.getByTestId('notif-approval-retired')).toHaveTextContent(refusedNotice())
    expect(store.getState().notifications.decidingApprovals ?? {}).toEqual({})
  })
  it('a 404 retires the row: buttons withdrawn, error notice, no DELETE, panel stays open', async () => {
    vi.mocked(api.decideApproval).mockRejectedValue(notFound())
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
    vi.mocked(api.decideApproval).mockRejectedValue(notFound())
    renderPanel()
    screen.getByRole('button', { name: /Approve/ }).focus()
    await userEvent.keyboard('{Enter}')
    await screen.findByTestId('notif-approval-retired')
    await waitFor(() => { expect(document.activeElement).toBe(screen.getByTestId('notif-approval-refusal-focus')) })
  })

  it('a 404 does not pull focus away from a reader who has moved on', async () => {
    let refuse: (e: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockImplementationOnce(() => new Promise((_, rej) => { refuse = rej }))
    renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    const markUnread = screen.getByRole('button', { name: /Mark unread/i })
    markUnread.focus()
    await act(async () => { refuse(notFound()) })
    await screen.findByTestId('notif-approval-retired')
    expect(document.activeElement).toBe(markUnread)
    await userEvent.click(markUnread)
  })

  it('a server expiry says the request was denied', () => {
    const { store } = renderPanel()
    act(() => { store.dispatch(retireApprovalNote({ ts: approvalNote.ts, why: 'expired' })) })
    const line = screen.getByTestId('notif-approval-retired')
    expect(line).toHaveTextContent(i18nT('hooks.useWebSocket.approval_wait_expired'))
    expect(line).not.toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
  })

  it('Dismiss on a retired row removes it once the server confirms, then closes', async () => {
    const { store, onClose } = renderPanel()
    act(() => { store.dispatch(retireApprovalNote({ ts: approvalNote.ts, why: 'gone' })) })
    await userEvent.click(screen.getByTestId('notif-decided-dismiss'))
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
    expect(store.getState().notifications.items).toEqual([])
  })

  it('a decision that lands but whose DELETE fails keeps the row and the panel, with the outcome', async () => {
    vi.mocked(api.decideApproval).mockResolvedValue({})
    vi.mocked(api.deleteNotification).mockRejectedValueOnce(new Error('network down'))
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.rejected'))
    await waitFor(() => { expect(screen.getByTestId('notif-decided-dismiss')).toBeEnabled() })
    expect(store.getState().notifications.items).toHaveLength(1)
    expect(onClose).not.toHaveBeenCalled()
    // The panel's own Dismiss retries the removal.
    await userEvent.click(screen.getByTestId('notif-decided-dismiss'))
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
    expect(store.getState().notifications.items).toEqual([])
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
    vi.mocked(api.decideApproval).mockImplementationOnce(() => new Promise(res => { settle = res }))
    const { onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    // The pressed button says it is working; the other only disables.
    const approving = screen.getByRole('button', { name: i18nT('components.notifications.notificationFeed.approving') })
    expect(approving).toBeDisabled()
    expect(approving).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('button', { name: /Reject/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Reject/ })).not.toHaveTextContent(i18nT('components.notifications.notificationFeed.rejecting'))
    await act(async () => { settle({}) })
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
    expect(api.decideApproval).toHaveBeenCalledTimes(1)
  })

  it('a non-terminal failure keeps both buttons and quotes the server reason', async () => {
    vi.mocked(api.decideApproval).mockRejectedValue(new ApiError(403, 'approval belongs to another session'))
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
    vi.mocked(api.decideApproval).mockRejectedValue(new TypeError('Failed to fetch'))
    renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(await screen.findByTestId('notif-approval-refusal')).toHaveTextContent(
      i18nT('components.approvalCard.decision_failed'),
    )
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
  })
})
