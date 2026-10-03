/**
 * A settled
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
import { retireApprovalNote, retireApprovalRow } from '../store/notificationsSlice'
import { refusedNotice } from '../components/notifications/notifMeta'

const mockResolveApproval = vi.fn().mockResolvedValue({})
const mockDeleteNotification = vi.fn().mockResolvedValue({})

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    deleteNotification: (...args: unknown[]) => mockDeleteNotification(...args),
    resolveApproval: (...args: unknown[]) => mockResolveApproval(...args),
    updateNotificationChannelSettings: vi.fn().mockResolvedValue({}),
  },
}))
// jsdom runs no animation frames reliably, so the erosion resolves at once.
// The erased row is left at opacity 0, as the real effect leaves it, so a
// failed DELETE has to put it back.
vi.mock('../lib/disintegrate', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/disintegrate')>(),
  disintegrate: (el: HTMLElement | null) => { if (el) el.style.opacity = '0'; return Promise.resolve() },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const approval: Notification = {
  kind: 'approval', ts: '1', title: 'Tool approval: shell', body: 'ls', approval_id: 'apr-1', acked: false,
}
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

  it('Approve/Reject are disabled in the view that pressed them while the decision is in flight', async () => {
    const d = deferred()
    mockResolveApproval.mockReturnValueOnce(d.promise)
    const { page } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    const approve = page.getByRole('button', { name: /^Approve$/ })
    expect(approve).toBeDisabled()
    expect(approve).toHaveAttribute('aria-busy', 'true')
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
      // It says the press failed, so it reads differently from the muted
      // line an expiry the server reported gets.
      expect(notice).toHaveTextContent(refusedNotice())
      expect(notice).not.toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
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

  it('a retired row leaves the unread count and is marked read on the server, with no DELETE', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.ackNotification).mockClear()
    const { store } = renderBoth([approval])
    act(() => { store.dispatch(retireApprovalRow('1', 'gone')) })
    expect(store.getState().notifications.items.filter(n => !n.acked)).toEqual([])
    expect(api.ackNotification).toHaveBeenCalledWith('1')
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it('a retired critical approval drops the red border and the unread dot in both views', () => {
    const { store, page, popover } = renderBoth([{ ...approval, priority: 'critical' }])
    const panelRow = () => page.getByText(approval.title).closest('[data-notif-row]') as HTMLElement
    expect(panelRow().className).toContain('border-l-danger')
    expect(panelRow().querySelector('[data-priority]')).not.toBeNull()
    // The mark alone, with the ack still unconfirmed, must be enough.
    act(() => { store.dispatch(retireApprovalNote({ ts: '1', why: 'reject' })) })
    expect(store.getState().notifications.items[0].acked).toBe(false)
    expect(panelRow().className).not.toContain('border-l-danger')
    expect(panelRow().querySelector('[data-priority]')).toBeNull()
    const card = popover.getByText(approval.title).closest('[data-notif-row]') as HTMLElement
    expect(card.querySelector('[data-priority]')).toBeNull()
  })

  it('a decision in flight in the page leaves the popover live; the server refusing the second retires the row', async () => {
    const d = deferred()
    mockResolveApproval.mockReturnValueOnce(d.promise).mockRejectedValueOnce(notFound())
    const { store, page, popover } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    // The in-flight state is the pressing view's own.
    expect(page.getByRole('button', { name: /^Reject$/ })).toBeDisabled()
    expect(popover.getByRole('button', { name: /^Reject$/ })).toBeEnabled()
    // The server accepts one decision per approval: the second press is
    // refused as no longer pending, and the row retires in every view.
    fireEvent.click(popover.getByRole('button', { name: /^Reject$/ }))
    await waitFor(() => { expect(store.getState().notifications.retiredApprovals).toEqual({ '1': 'refused' }) })
    expect(page.queryByRole('button', { name: /^Approve$/ })).toBeNull()
    expect(popover.queryByRole('button', { name: /^Reject$/ })).toBeNull()
    await act(async () => { d.reject(new ApiError(500, 'boom')) })
  })

  it('a landed decision whose DELETE fails keeps the row with its outcome and says the dismiss failed', async () => {
    mockDeleteNotification.mockRejectedValueOnce(new ApiError(500, 'boom'))
    const { store, page, popover } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    await waitFor(() => { expect(store.getState().notifications.dismissFailed).toEqual({ '1': true }) })
    for (const view of [page, popover]) {
      const row = view.getByText(approval.title).closest('[data-notif-row]') as HTMLElement
      expect(within(row).getByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approved'))
      expect(within(row).getByTestId('notif-dismiss-failed')).toHaveAttribute('role', 'alert')
      expect(within(row).getByTestId('notif-dismiss-failed')).toHaveTextContent(i18nT('components.notifications.notificationFeed.could_not_dismiss_try_again'))
    }
    // A retry that lands clears it, and the row leaves.
    fireEvent.click(page.getAllByRole('button', { name: DISMISS() })[0])
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
  })

  it('a close X whose DELETE fails brings the row back with the failure notice', async () => {
    mockDeleteNotification.mockRejectedValueOnce(new ApiError(500, 'boom'))
    const store = createTestStore({ notifications: { items: [{ ...approval, acked: true }], retiredApprovals: { '1': 'gone' } } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    const notice = await screen.findByTestId('notif-dismiss-failed')
    const row = notice.closest('[data-notif-row]') as HTMLElement
    expect(row.getAttribute('data-ts')).toBe('1')
    expect(row.style.opacity).toBe('')
    expect(store.getState().notifications.items).toHaveLength(1)
  })

  it('a retired row shows its close X without a hover, in both variants', () => {
    const store = createTestStore({ notifications: { items: [{ ...approval, acked: true }], retiredApprovals: { '1': 'gone' } } as RootState['notifications'] })
    for (const variant of ['panel', 'mac'] as const) {
      const { unmount } = renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant={variant} />, { store })
      const close = screen.getByRole('button', { name: DISMISS() })
      expect(close.className).toContain('opacity-60')
      expect(close.className).not.toMatch(/(^|\s)opacity-0(\s|$)/)
      unmount()
    }
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
    expect(mockResolveApproval).toHaveBeenCalledWith('apr-1', 'approve', { origin: 'coordinator', slot: 'chat-1', instance: 'inst-b' })
    await waitFor(() => { expect(store.getState().notifications.retiredApprovals).toEqual({ '1': 'gone', '3': 'approve' }) })
  })

  it('a row already retired when the feed mounts is dismissed with its close X', async () => {
    const store = createTestStore({ notifications: { items: [{ ...approval, acked: true }], retiredApprovals: { '1': 'gone' } } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(mockDeleteNotification).toHaveBeenCalledWith('1') })
  })
})
