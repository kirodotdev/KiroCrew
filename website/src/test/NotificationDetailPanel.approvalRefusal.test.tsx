/**
 * The detail panel's Approve/Reject read the same store marks as the feed.
 * A terminal refusal (the approval expired or was already decided elsewhere)
 * RETIRES the row: both buttons are withdrawn and an error notice replaces
 * them; the row leaves through the feed row's close X. A decision that lands
 * removes the row and closes the panel. An approval row is this tab's own copy
 * (the server keeps no approval note), so neither sends a DELETE.
 * Any other decision failure keeps the buttons, because the approval may
 * still be pending.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders, createTestStore } from './helpers'
import { ThemeBootProbe, themeBootSettled } from './themeBootSettled'
import type { RootState } from '../store'
import { approvalDecisionSettled, clearAllNotifications, endApprovalRow, fetchNotifications } from '../store/notificationsSlice'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
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
  body: 'gh issue view 14704', approval_id: 'apr-gone', approval_instance: 'inst-gone', acked: true, _local: true,
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
  it('a decision that lands removes the row and closes the panel', async () => {
    vi.mocked(api.decideApproval).mockResolvedValue({})
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
    expect(store.getState().notifications.retiredApprovals ?? {}).toEqual({})
    // The row was this tab's own copy: no request, so nothing can fail.
    expect(api.deleteNotification).not.toHaveBeenCalled()
  })
  it('a row that names no request sends nothing and retires as refused', async () => {
    const { approval_instance: _none, ...n } = approvalNote
    const store = createTestStore({ notifications: { items: [n] } as RootState['notifications'] })
    renderWithProviders(<NotificationDetailPanel n={n} onClose={vi.fn()} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(api.decideApproval).not.toHaveBeenCalled()
    expect(store.getState().notifications.retiredApprovals).toEqual({ [n.ts]: true })
    expect(screen.getByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
    expect(store.getState().notifications.decidingApprovals ?? {}).toEqual({})
  })
  it('a 404 retires the row: buttons withdrawn, error notice, no DELETE, panel stays open', async () => {
    vi.mocked(api.decideApproval).mockRejectedValue(notFound())
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Reject/ })).toBeNull()
    expect(store.getState().notifications.retiredApprovals).toEqual({ [approvalNote.ts]: true })
    // The decide failed, so the sentence is an error notice, not muted status.
    expect(screen.getByTestId('notif-approval-retired')).toHaveAttribute('role', 'alert')
    expect(api.deleteNotification).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('a decision on a stored note whose DELETE fails keeps the panel open with dismiss_failed and no buttons', async () => {
    vi.mocked(api.decideApproval).mockResolvedValue({})
    vi.mocked(api.deleteNotification).mockRejectedValueOnce(new Error('offline'))
    const stored: Notification = { ...approvalNote, _local: undefined }
    const store = createTestStore({ notifications: { items: [stored] } as RootState['notifications'] })
    const onClose = vi.fn()
    renderWithProviders(<NotificationDetailPanel n={stored} onClose={onClose} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(await screen.findByTestId('notif-dismiss-failed')).toHaveTextContent(i18nT('components.notifications.notificationFeed.decided_dismiss_failed'))
    expect(screen.getByTestId('notif-dismiss-failed')).toHaveAttribute('role', 'alert')
    // The withdrawn row's one action is named for what it does.
    expect(screen.getByTestId('notif-detail-close')).toHaveTextContent(i18nT('components.notifications.notificationFeed.dismiss_notification'))
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Reject/ })).toBeNull()
    expect(screen.queryByTestId('notif-approval-retired')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([stored.ts])
  })
  it('an expiry frame during the decision keeps the panel row and shows the refusal', async () => {
    let reject: (e: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockReturnValueOnce(new Promise((_res, rej) => { reject = rej }))
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    act(() => { store.dispatch(endApprovalRow(approvalNote.ts)) })
    await act(async () => { reject(notFound()) })
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([approvalNote.ts])
    expect(onClose).not.toHaveBeenCalled()
  })
  it('the retired row\'s Dismiss also takes the row out of the feed', async () => {
    const { store, onClose } = renderPanel()
    act(() => { store.dispatch(approvalDecisionSettled({ ts: approvalNote.ts, outcome: 'refused' })) })
    await userEvent.click(screen.getByTestId('notif-detail-close'))
    expect(onClose).toHaveBeenCalledTimes(1)
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
  })
  it('Close on a live row leaves the row in the feed', async () => {
    const { store, onClose } = renderPanel()
    expect(screen.getByTestId('notif-detail-close')).toHaveTextContent(i18nT('components.notifications.notificationDetailPanel.close'))
    await userEvent.click(screen.getByTestId('notif-detail-close'))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([approvalNote.ts])
  })

  it('a 404 moves keyboard focus to the panel\'s Close once the pressed button is withdrawn', async () => {
    vi.mocked(api.decideApproval).mockRejectedValue(notFound())
    renderPanel()
    screen.getByRole('button', { name: /Approve/ }).focus()
    await userEvent.keyboard('{Enter}')
    await screen.findByTestId('notif-approval-retired')
    await waitFor(() => { expect(document.activeElement).toBe(screen.getByTestId('notif-detail-close')) })
  })

  it('a 404 does not pull focus away from a reader who has moved on', async () => {
    let refuse: (e: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockImplementationOnce(() => new Promise((_, rej) => { refuse = rej }))
    renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    // The reader has moved on to the panel's own Close.
    const close = screen.getByRole('button', { name: /^Close$/i })
    close.focus()
    await act(async () => { refuse(notFound()) })
    await screen.findByTestId('notif-approval-retired')
    expect(document.activeElement).toBe(close)
  })

  it('a retired row offers no read toggle, which would change nothing', () => {
    const { store } = renderPanel()
    act(() => { store.dispatch(approvalDecisionSettled({ ts: approvalNote.ts, outcome: 'refused' })) })
    expect(screen.queryByText(i18nT('components.notifications.notificationDetailPanel.mark_unread'))).toBeNull()
    expect(screen.queryByText(i18nT('components.notifications.notificationDetailPanel.mark_read'))).toBeNull()
  })

  it('a remounted panel over a retired row still withdraws the buttons', async () => {
    const store = createTestStore({ notifications: {
      items: [approvalNote], retiredApprovals: { [approvalNote.ts]: true },
    } as RootState['notifications'] })
    renderWithProviders(<><NotificationDetailPanel n={approvalNote} onClose={() => {}} /><ThemeBootProbe /></>, { store })
    await themeBootSettled()
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
    expect(screen.getByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
  })

  it('a non-terminal refusal from the decide route keeps both buttons and quotes its reason', async () => {
    vi.mocked(api.decideApproval).mockRejectedValue(new ApiError(400, 'invalid approval target'))
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    expect(await screen.findByTestId('notif-approval-refusal')).toHaveTextContent(
      i18nT('components.approvalCard.decision_not_recorded_error', { error: 'invalid approval target' }),
    )
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reject/ })).toBeTruthy()
    expect(store.getState().notifications.retiredApprovals ?? {}).toEqual({})
    // The panel stays open, so the notice and the retry stay in view.
    expect(onClose).not.toHaveBeenCalled()
  })

  it.each([503, 502, 429])('a %i keeps the buttons and says it in plain words, never the layer\'s own text', async (status) => {
    vi.mocked(api.decideApproval).mockRejectedValue(new ApiError(status, 'upstream proxy overloaded'))
    const { onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    const notice = await screen.findByTestId('notif-approval-refusal')
    expect(notice).toHaveTextContent(i18nT('components.approvalCard.decision_failed'))
    expect(notice.textContent).not.toContain('upstream proxy overloaded')
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('a transport failure with no response keeps the buttons and says the decision may not have landed', async () => {
    vi.mocked(api.decideApproval).mockRejectedValue(new TypeError('Failed to fetch'))
    const { onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(await screen.findByTestId('notif-approval-refusal')).toHaveTextContent(
      i18nT('components.approvalCard.decision_failed'),
    )
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it.each([
    ['a reconnect snapshot', fetchNotifications.fulfilled({ items: [], seq: 0, ackSeq: 0 }, '', undefined)],
    ['a clear-all', clearAllNotifications()],
  ])('%s that omits a deciding row keeps it, so a late 404 still renders the refusal', async (_label, replace) => {
    let refuse: (e: unknown) => void = () => {}
    vi.mocked(api.decideApproval).mockReturnValue(new Promise((_res, rej) => { refuse = rej }))
    // clearSeq 0 matches the snapshot's, so the reducer applies it.
    const store = createTestStore({ notifications: { items: [approvalNote], clearSeq: 0 } as RootState['notifications'] })
    const onClose = vi.fn()
    renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={onClose} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    act(() => { store.dispatch(replace) })
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([approvalNote.ts])
    expect(store.getState().notifications.approvalDecisions?.[approvalNote.ts]?.inFlight).toBe(1)
    await act(async () => { refuse(notFound()) })
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
    expect(store.getState().notifications.retiredApprovals).toEqual({ [approvalNote.ts]: true })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('a cron note whose DELETE failed keeps its read toggle and Close, since only an approval row explains a withdrawal', async () => {
    const cron: Notification = { kind: 'cron', ts: '2026-09-28T00:30:00Z', title: 'Nightly report finished', body: 'ok', acked: false }
    const store = createTestStore({ notifications: { items: [cron], dismissFailed: { [cron.ts]: 'dismiss' } } as RootState['notifications'] })
    const onClose = vi.fn()
    renderWithProviders(<NotificationDetailPanel n={cron} onClose={onClose} />, { store })
    expect(screen.getByText(i18nT('components.notifications.notificationDetailPanel.mark_read'))).toBeInTheDocument()
    const close = screen.getByTestId('notif-detail-close')
    expect(close).toHaveTextContent(i18nT('components.notifications.notificationDetailPanel.close'))
    await userEvent.click(close)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(api.deleteNotification).not.toHaveBeenCalled()
  })
})
