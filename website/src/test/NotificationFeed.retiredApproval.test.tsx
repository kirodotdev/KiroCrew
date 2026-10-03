/**
 * The server is the source of truth for which notifications exist. A settled
 * approval (an `approval_resolved` frame, a decision that landed, a 404 on
 * decide) is only RETIRED in the store: its row stays, loses Approve/Reject,
 * says why, and keeps its ordinary close X. The page feed and the bell popover
 * mount the same component on one store, so both read the same marks.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import type { RootState } from '../store'
import type { Notification } from '../types'
import { i18nT } from '../i18n/t'
import { ApiError } from '../api/apiError'
import { retireApprovalNote, retireApprovalRow, ackNotificationByTs, selectUnreadNotes } from '../store/notificationsSlice'
import { approvalTargetKey, type CoordinatorApprovalTarget } from '../types/approvalTarget'
import { refusedNotice } from '../components/notifications/notifMeta'

const mockResolveApproval = vi.fn().mockResolvedValue({})
const mockDeleteNotification = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    deleteNotification: (...args: unknown[]) => mockDeleteNotification(...args),
    decideApproval: (...args: unknown[]) => mockResolveApproval(...args),
    updateNotificationChannelSettings: vi.fn().mockResolvedValue({}),
  },
}))
// The erosion runs only after the server confirms; resolve it at once here
// (jsdom runs no animation frames reliably).
vi.mock('../lib/disintegrate', () => ({ disintegrate: () => Promise.resolve() }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const approval: Notification = {
  kind: 'approval', ts: '1', title: 'Tool approval: shell', body: 'ls', approval_id: 'apr-1', approval_instance: 'inst-1', acked: false,
}
/** The request the base row names: a slotless coordinator approval. */
const TARGET: CoordinatorApprovalTarget = { origin: 'coordinator', id: 'apr-1', slot: '', instance: 'inst-1' }
const notFound = () => Object.assign(new Error('not found or expired'), { status: 404 })
const DISMISS = () => i18nT('components.notifications.notificationFeed.dismiss_notification')

/** The page feed and the bell popover, side by side on one store. */
function renderBoth(items: Notification[]) {
  const store = createTestStore({ notifications: { items } as RootState['notifications'] })
  const view = renderWithProviders(
    <>
      <div data-testid="page"><NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" /></div>
      <div data-testid="popover"><NotificationFeed selectedTs={null} onSelect={() => {}} variant="mac" /></div>
    </>,
    { store },
  )
  return { ...view, page: within(screen.getByTestId('page')), popover: within(screen.getByTestId('popover')) }
}

function deferred() {
  let resolve: (v: unknown) => void = () => {}
  let reject: (e: unknown) => void = () => {}
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => {
  localStorage.clear()
  mockResolveApproval.mockReset()
  mockResolveApproval.mockResolvedValue({})
  mockDeleteNotification.mockReset()
  mockDeleteNotification.mockResolvedValue({})
})

describe('NotificationFeed retired approvals', () => {
  it('an approval_resolved retirement keeps the row in both views, without Approve/Reject', () => {
    const { store, page, popover } = renderBoth([approval])
    act(() => { store.dispatch(retireApprovalNote({ ts: '1', why: 'gone' })) })
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual(['1'])
    for (const view of [page, popover]) {
      expect(view.getByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
      expect(view.queryByRole('button', { name: /^Approve$/ })).toBeNull()
      expect(view.queryByRole('button', { name: /^Reject$/ })).toBeNull()
      // The row keeps its ordinary close control; nothing new takes Approve's place.
      expect(view.getByRole('button', { name: DISMISS() })).toBeInTheDocument()
      expect(view.queryByTestId('notif-retired-dismiss')).toBeNull()
    }
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it('a server expiry says the request was denied in both views, not "expired or already decided"', () => {
    const { store, page, popover } = renderBoth([approval])
    act(() => { store.dispatch(retireApprovalNote({ ts: '1', why: 'expired' })) })
    for (const view of [page, popover]) {
      const line = view.getByTestId('notif-approval-retired')
      expect(line).toHaveTextContent(i18nT('hooks.useWebSocket.approval_wait_expired'))
      expect(line).not.toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
      expect(view.queryByRole('button', { name: /^Approve$/ })).toBeNull()
    }
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it('a 404 on decide retires the row and sends no DELETE; its close X then removes it', async () => {
    mockResolveApproval.mockRejectedValueOnce(notFound())
    const { store, page } = renderBoth([approval])
    const approve = page.getByRole('button', { name: /^Approve$/ })
    approve.focus()
    fireEvent.click(approve)
    await page.findByTestId('notif-approval-retired')
    expect(mockDeleteNotification).not.toHaveBeenCalled()
    // Focus moves to the row's own open control instead of falling to <body>.
    const row = page.getByTestId('notif-approval-retired').closest('[data-notif-row]') as HTMLElement
    await waitFor(() => { expect(document.activeElement).toBe(row.querySelector('[data-notif-open]')) })
    fireEvent.click(within(row).getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
    expect(mockDeleteNotification).toHaveBeenCalledTimes(1)
  })

  it('a retryable decision failure keeps the buttons and says so on the row it belongs to', async () => {
    mockResolveApproval.mockRejectedValueOnce(new ApiError(500, 'boom'))
    const { store, page } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Reject$/ }))
    const notice = await page.findByTestId('notif-approval-notice')
    expect(notice.closest('[data-notif-row]')?.getAttribute('data-ts')).toBe('1')
    expect(notice).toHaveTextContent(i18nT('components.approvalCard.decision_not_recorded_error', { error: 'boom' }))
    expect(page.getByRole('button', { name: /^Approve$/ })).toBeEnabled()
    expect(store.getState().notifications.retiredApprovals ?? {}).toEqual({})
  })

  it('a retryable failure renders below the still-live Approve/Reject in both variants', async () => {
    mockResolveApproval.mockRejectedValue(new ApiError(500, 'boom'))
    const { page, popover } = renderBoth([approval])
    for (const view of [page, popover]) {
      fireEvent.click(view.getByRole('button', { name: /^Reject$/ }))
      const notice = await view.findByTestId('notif-approval-notice')
      // The retry buttons stay where they were: the notice follows them.
      for (const name of [/^Approve$/, /^Reject$/]) {
        const button = view.getByRole('button', { name })
        expect(button.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      }
    }
  })

  it('Approve/Reject are disabled while the decision is in flight', async () => {
    const d = deferred()
    mockResolveApproval.mockReturnValueOnce(d.promise)
    const { page } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    // The pressed button says it is working; the other only disables.
    const approving = page.getByRole('button', { name: i18nT('components.notifications.notificationFeed.approving') })
    expect(approving).toBeDisabled()
    expect(approving).toHaveAttribute('aria-busy', 'true')
    expect(page.queryByRole('button', { name: /^Approve$/ })).toBeNull()
    expect(page.getByRole('button', { name: /^Reject$/ })).toBeDisabled()
    fireEvent.click(page.getByRole('button', { name: /^Reject$/ }))
    expect(mockResolveApproval).toHaveBeenCalledTimes(1)
    await act(async () => { d.reject(new ApiError(500, 'boom')) })
    expect(page.getByRole('button', { name: /^Approve$/ })).toBeEnabled()
  })

  it('a 404 on decide says "no longer pending" through the error notice, in both views', async () => {
    mockResolveApproval.mockRejectedValueOnce(notFound())
    const { page, popover } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    await page.findByTestId('notif-approval-retired')
    for (const view of [page, popover]) {
      const notice = view.getByTestId('notif-approval-retired')
      // The request failed, so it is an error (role=alert), not muted status.
      expect(notice).toHaveAttribute('role', 'alert')
      // The chat card's sentence for the same refusal, one sentence.
      expect(notice).toHaveTextContent(refusedNotice())
      expect(notice).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
      expect(notice.textContent).not.toContain(i18nT('components.approvalCard.decision_not_recorded_error', { error: '' }).trim())
    }
  })

  it('an expiry the server reported stays a neutral status line', () => {
    const { store, page } = renderBoth([approval])
    act(() => { store.dispatch(retireApprovalNote({ ts: '1', why: 'gone' })) })
    expect(page.getByTestId('notif-approval-retired')).toHaveAttribute('role', 'status')
  })

  it('after a decision lands, focus goes to the row while the DELETE is pending, then to the list', async () => {
    const d = deferred()
    mockDeleteNotification.mockReturnValueOnce(d.promise)
    const { page } = renderBoth([approval])
    const approve = page.getByRole('button', { name: /^Approve$/ })
    approve.focus()
    fireEvent.click(approve)
    await waitFor(() => { expect(document.activeElement).not.toBe(document.body) })
    const focused = document.activeElement as HTMLElement
    expect(focused.closest('[data-notif-row]')?.getAttribute('data-ts')).toBe('1')
    await act(async () => { d.resolve({ ok: true }) })
    await waitFor(() => { expect(document.activeElement).toBe(page.getByTestId('notification-feed-list')) })
  })

  it('an approval that retires unseen stays unread, sends no ack and no DELETE', async () => {
    // An expiry while the reader was away: the job was denied, and the bell
    // must still say so until someone looks.
    const { api } = await import('../api/client')
    vi.mocked(api.ackNotification).mockClear()
    const { store } = renderBoth([approval])
    act(() => { store.dispatch(retireApprovalRow('1', 'gone')) })
    expect(selectUnreadNotes(store.getState()).map(n => n.ts)).toEqual(['1'])
    expect(api.ackNotification).not.toHaveBeenCalled()
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it("the reader's own refused press reads the row: they are looking at the outcome", async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.ackNotification).mockClear()
    mockResolveApproval.mockRejectedValueOnce(new ApiError(404, 'not found or expired'))
    const { page } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    await waitFor(() => expect(api.ackNotification).toHaveBeenCalledWith('1'))
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it('a retired critical approval drops the red border and keeps a quiet unread dot until read', () => {
    const { store, page, popover } = renderBoth([{ ...approval, priority: 'critical' }])
    const panelRow = () => page.getByText(approval.title).closest('[data-notif-row]') as HTMLElement
    const card = () => popover.getByText(approval.title).closest('[data-notif-row]') as HTMLElement
    expect(panelRow().className).toContain('border-l-danger')
    expect(panelRow().querySelector('[data-priority]')?.getAttribute('data-priority')).toBe('critical')
    act(() => { store.dispatch(retireApprovalNote({ ts: '1', why: 'reject' })) })
    expect(panelRow().className).not.toContain('border-l-danger')
    // Unread, so a dot stays, but the settled one: no danger tint, no pulse.
    for (const row of [panelRow(), card()]) {
      const dot = row.querySelector('[data-priority]')
      expect(dot?.getAttribute('data-priority')).toBe('settled')
      expect(dot?.className).not.toContain('bg-danger')
      expect(dot?.className).not.toContain('animate-dot-breathe')
    }
    act(() => { store.dispatch(ackNotificationByTs('1')) })
    expect(panelRow().querySelector('[data-priority]')).toBeNull()
    expect(card().querySelector('[data-priority]')).toBeNull()
  })

  it('a decision in flight from the page disables the popover too, so only one decide is sent', async () => {
    const d = deferred()
    mockResolveApproval.mockReturnValueOnce(d.promise)
    const { store, page, popover } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    // Claimed under the request the row names, not its bare id.
    expect(store.getState().notifications.decidingApprovals).toEqual({ [approvalTargetKey(TARGET)]: 'approve' })
    // Every view labels the pressed action, not only the one it was pressed in.
    expect(popover.getByRole('button', { name: i18nT('components.notifications.notificationFeed.approving') })).toBeDisabled()
    expect(popover.getByRole('button', { name: /^Reject$/ })).toBeDisabled()
    // Even a press that reaches the handler is refused by the claim.
    fireEvent.click(popover.getByRole('button', { name: /^Reject$/ }))
    expect(mockResolveApproval).toHaveBeenCalledTimes(1)
    expect(mockResolveApproval).toHaveBeenCalledWith(TARGET, 'approve')
    await act(async () => { d.reject(new ApiError(500, 'boom')) })
    expect(store.getState().notifications.decidingApprovals).toEqual({})
    expect(popover.getByRole('button', { name: /^Reject$/ })).toBeEnabled()
  })

  it('the bell popover decides the request its row names', async () => {
    const row: Notification = { ...approval, approval_instance: 'inst-pop', slot: 'chat-2' }
    const store = createTestStore({ notifications: { items: [row] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="mac" />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Reject$/ }))
    expect(mockResolveApproval).toHaveBeenCalledWith({ origin: 'coordinator', id: 'apr-1', slot: 'chat-2', instance: 'inst-pop' }, 'reject')
    await waitFor(() => { expect(store.getState().notifications.retiredApprovals).toEqual({ '1': 'reject' }) })
  })

  it.each(['panel', 'mac'] as const)('a row that names no request sends nothing and retires as refused (%s)', async variant => {
    // Written by an older build without the instance: no request can be named,
    // so no id is sent in its place.
    const { approval_instance: _none, ...row } = approval
    const store = createTestStore({ notifications: { items: [row] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant={variant} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    expect(mockResolveApproval).not.toHaveBeenCalled()
    await waitFor(() => { expect(store.getState().notifications.retiredApprovals).toEqual({ '1': 'refused' }) })
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(refusedNotice())
    expect(store.getState().notifications.decidingApprovals ?? {}).toEqual({})
  })

  it('a decide on the live row of a recurring id is bound to that row\'s own instance', async () => {
    // Request A retired; the caller reused its id for request B.
    const rowA: Notification = { ...approval, approval_instance: 'inst-a' }
    const rowB: Notification = { ...approval, ts: '3', title: 'Tool approval: shell again', approval_instance: 'inst-b', slot: 'chat-1' }
    const store = createTestStore({
      notifications: { items: [rowA, rowB], retiredApprovals: { '1': 'gone' } } as RootState['notifications'],
    })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    expect(mockResolveApproval).toHaveBeenCalledTimes(1)
    expect(mockResolveApproval).toHaveBeenCalledWith({ origin: 'coordinator', id: 'apr-1', slot: 'chat-1', instance: 'inst-b' }, 'approve')
    await waitFor(() => { expect(store.getState().notifications.retiredApprovals).toEqual({ '1': 'gone', '3': 'approve' }) })
  })

  it('a row already retired when the feed mounts is dismissed with its close X', async () => {
    const store = createTestStore({ notifications: { items: [{ ...approval, acked: true }], retiredApprovals: { '1': 'gone' } } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(mockDeleteNotification).toHaveBeenCalledWith('1') })
  })
})
