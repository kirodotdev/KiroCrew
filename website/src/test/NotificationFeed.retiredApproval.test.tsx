/**
 * An approval whose decide was refused as no longer pending (it expired or was decided
 * elsewhere) is only RETIRED in the store: its row stays, loses
 * Approve/Reject, says why, and keeps its ordinary close X. A decision that
 * lands removes the row. The page feed and the bell popover
 * mount the same component on one store, so both read the same marks.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { ThemeBootProbe, themeBootSettled } from './themeBootSettled'
import NotificationFeed from '../components/notifications/NotificationFeed'
import type { RootState } from '../store'
import type { Notification } from '../types'
import { i18nT } from '../i18n/t'
import { ApiError } from '../api/apiError'
import { approvalDecisionSettled, ackNotificationByTs, unackNotificationByTs, endApprovalRow } from '../store/notificationsSlice'

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
// jsdom runs no animation frames reliably, so the erosion resolves at once.
vi.mock('../lib/disintegrate', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/disintegrate')>(),
  disintegrate: (el: HTMLElement | null) => { if (el) el.style.opacity = '0'; return Promise.resolve() },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const approval: Notification = {
  kind: 'approval', ts: '1', title: 'Tool approval: shell', body: 'ls', approval_id: 'apr-1', approval_instance: 'inst-1', acked: false, _local: true,
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
  it('a retired row keeps its place in both views, without Approve/Reject', () => {
    const { store, page, popover } = renderBoth([approval])
    act(() => { store.dispatch(approvalDecisionSettled({ ts: '1', outcome: 'refused' })) })
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

  it('a retired row brings its notice into view', async () => {
    // The list fades at its scroll edge, so a notice on the last visible row
    // would otherwise lose its tail under the fade.
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    try {
      mockResolveApproval.mockRejectedValueOnce(notFound())
      const { page } = renderBoth([approval])
      fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
      const notice = await page.findByTestId('notif-approval-retired')
      await waitFor(() => { expect(scrolled).toContain(notice) })
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('a retryable failure brings its notice into view, in both variants', async () => {
    // Same scroll fade as above: the failure notice on a bottom row would be
    // cut off mid-sentence, and the press would look like it did nothing.
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    try {
      for (const name of ['page', 'popover'] as const) {
        mockResolveApproval.mockRejectedValueOnce(new ApiError(500, 'boom'))
        const views = renderBoth([approval])
        const view = views[name]
        fireEvent.click(view.getByRole('button', { name: /^Approve$/ }))
        const notice = await view.findByTestId('notif-approval-notice')
        expect(notice).toHaveTextContent(i18nT('components.approvalCard.decision_failed'))
        await waitFor(() => { expect(scrolled).toContain(notice) })
        views.unmount()
      }
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })

  it('a 404 on decide retires the row and sends no DELETE; its close X then removes it', async () => {
    mockResolveApproval.mockRejectedValueOnce(notFound())
    const { store, page } = renderBoth([approval])
    const approve = page.getByRole('button', { name: /^Approve$/ })
    approve.focus()
    fireEvent.click(approve)
    await page.findByTestId('notif-approval-retired')
    expect(mockDeleteNotification).not.toHaveBeenCalled()
    // Focus moves to the row's close X instead of falling to <body>.
    const row = page.getByTestId('notif-approval-retired').closest('[data-notif-row]') as HTMLElement
    await waitFor(() => { expect(document.activeElement).toBe(row.querySelector('[data-notif-dismiss]')) })
    fireEvent.click(within(row).getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
    // The row is this tab's own copy: it leaves locally, with no request.
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it('a retryable decision failure keeps the buttons and says so on the row it belongs to', async () => {
    mockResolveApproval.mockRejectedValueOnce(new ApiError(500, 'boom'))
    const { store, page } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Reject$/ }))
    const notice = await page.findByTestId('notif-approval-notice')
    expect(notice.closest('[data-notif-row]')?.getAttribute('data-ts')).toBe('1')
    // A 5xx is the layer between, in its own words: the row says it plainly.
    expect(notice).toHaveTextContent(i18nT('components.approvalCard.decision_failed'))
    expect(notice.textContent).not.toContain('boom')
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

  it('a 404 on decide says "no longer pending" through the error notice, in both views', async () => {
    mockResolveApproval.mockRejectedValueOnce(notFound())
    const { page, popover } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    await page.findByTestId('notif-approval-retired')
    for (const view of [page, popover]) {
      const notice = view.getByTestId('notif-approval-retired')
      // The request failed, so it is an error (role=alert), not muted status.
      expect(notice).toHaveAttribute('role', 'alert')
      // The same sentence the chat card, tool group, Subagents panel and
      // command center show for a refused press.
      expect(notice).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
      expect(notice.textContent).not.toContain(i18nT('components.approvalCard.decision_not_recorded_error', { error: '' }).trim())
    }
  })

  it('a decision that lands removes the row from both views, retiring nothing', async () => {
    const { store, page, popover } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
    expect(mockDeleteNotification).not.toHaveBeenCalled()
    expect(store.getState().notifications.retiredApprovals ?? {}).toEqual({})
    for (const view of [page, popover]) expect(view.queryByText(approval.title)).toBeNull()
  })

  it('a retired row leaves the unread count with nothing sent to the server', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.ackNotification).mockClear()
    const { store } = renderBoth([approval])
    act(() => { store.dispatch(approvalDecisionSettled({ ts: '1', outcome: 'refused' })) })
    expect(store.getState().notifications.items.filter(n => !n.acked)).toEqual([])
    // The server holds no approval note, so there is nothing to ack or delete.
    expect(api.ackNotification).not.toHaveBeenCalled()
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it("the reader's own refused press reads the row: they are looking at the outcome", async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.ackNotification).mockClear()
    mockResolveApproval.mockRejectedValueOnce(new ApiError(404, 'not found or expired'))
    const { store, page } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    await waitFor(() => expect(store.getState().notifications.items[0].acked).toBe(true))
    // Read in this tab only: the server holds no approval note to ack.
    expect(api.ackNotification).not.toHaveBeenCalled()
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it('a retired critical approval drops the red border and keeps a quiet unread dot until read', () => {
    const { store, page, popover } = renderBoth([{ ...approval, priority: 'critical' }])
    const panelRow = () => page.getByText(approval.title).closest('[data-notif-row]') as HTMLElement
    const card = () => popover.getByText(approval.title).closest('[data-notif-row]') as HTMLElement
    expect(panelRow().className).toContain('border-l-danger')
    expect(panelRow().querySelector('[data-priority]')?.getAttribute('data-priority')).toBe('critical')
    // A retired row that reads as unread (an unack clears the read mark
    // retirement sets) keeps only a quiet dot.
    act(() => { store.dispatch(approvalDecisionSettled({ ts: '1', outcome: 'refused' })) })
    act(() => { store.dispatch(unackNotificationByTs('1')) })
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

  it('a second decision while the first is in flight is refused by the server and retires the row', async () => {
    const d = deferred()
    mockResolveApproval.mockReturnValueOnce(d.promise).mockRejectedValueOnce(notFound())
    const { store, page, popover } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    // The server accepts one decision per approval: the second press is
    // refused as no longer pending, and the row retires in every view.
    fireEvent.click(popover.getByRole('button', { name: /^Reject$/ }))
    await waitFor(() => { expect(store.getState().notifications.retiredApprovals).toEqual({ '1': true }) })
    expect(page.queryByRole('button', { name: /^Approve$/ })).toBeNull()
    expect(popover.queryByRole('button', { name: /^Reject$/ })).toBeNull()
    await act(async () => { d.reject(new ApiError(500, 'boom')) })
  })

  it('an approve racing the expiry frame keeps the row and shows the refusal once the 404 lands', async () => {
    const d = deferred()
    mockResolveApproval.mockReturnValueOnce(d.promise)
    const { store, page, popover } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    // The cron expiry frame lands while the POST is still in flight.
    act(() => { store.dispatch(endApprovalRow('1')) })
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual(['1'])
    await act(async () => { d.reject(notFound()) })
    await waitFor(() => { expect(store.getState().notifications.retiredApprovals).toEqual({ '1': true }) })
    expect(popover.getByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
    expect(page.getByTestId('notif-approval-retired')).toHaveAttribute('role', 'alert')
    expect(store.getState().notifications.approvalDecisions).toEqual({})
  })
  it('an expiry frame on an idle row removes it at once', () => {
    const { store } = renderBoth([approval])
    act(() => { store.dispatch(endApprovalRow('1')) })
    expect(store.getState().notifications.items).toEqual([])
  })
  it('a retryable failure after the expiry frame lets the row leave, as the frame said', async () => {
    const d = deferred()
    mockResolveApproval.mockReturnValueOnce(d.promise)
    const { store, page } = renderBoth([approval])
    fireEvent.click(page.getByRole('button', { name: /^Approve$/ }))
    act(() => { store.dispatch(endApprovalRow('1')) })
    await act(async () => { d.reject(new ApiError(503, 'gateway busy, try again')) })
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
  })
  it('a decision that lands on a stored approval note whose DELETE fails withdraws the buttons and says dismiss_failed', async () => {
    const note: Notification = { kind: 'approval', ts: '8', title: 'App approval note', body: 'x', approval_id: 'apr-8', approval_instance: 'inst-8', acked: false }
    mockDeleteNotification.mockRejectedValueOnce(new Error('offline'))
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    expect(await screen.findByTestId('notif-dismiss-failed')).toHaveTextContent(i18nT('components.notifications.notificationFeed.decided_dismiss_failed'))
    expect(screen.queryByRole('button', { name: /^Approve$/ })).toBeNull()
    expect(store.getState().notifications.dismissFailed).toEqual({ '8': 'decided' })
    // The close X is shown without a hover, since it is the retry.
    expect(screen.getByRole('button', { name: DISMISS() }).className).toContain('opacity-80')
    expect(store.getState().notifications.retiredApprovals ?? {}).toEqual({})
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
  })

  it('the open approval row leaves its failed-DELETE notice to the detail panel; a closed row and a cron note show it', async () => {
    const note: Notification = { kind: 'approval', ts: '8', title: 'App approval note', body: 'x', approval_id: 'apr-8', acked: false }
    const cron: Notification = { kind: 'cron', ts: '9', title: 'Cron note', body: 'y', acked: false }
    const store = createTestStore({ notifications: { items: [note, cron], dismissFailed: { '8': 'decided', '9': 'dismiss' } } as RootState['notifications'] })
    const { unmount } = renderWithProviders(<><NotificationFeed selectedTs="8" onSelect={() => {}} variant="panel" /><ThemeBootProbe /></>, { store })
    await themeBootSettled()
    const shown = () => screen.queryAllByTestId('notif-dismiss-failed').map(el => el.closest('[data-notif-row]')?.getAttribute('data-ts'))
    expect(shown()).toEqual(['9'])
    unmount()
    renderWithProviders(<><NotificationFeed selectedTs="9" onSelect={() => {}} variant="panel" /><ThemeBootProbe /></>, { store })
    await themeBootSettled()
    // The panel shows no notice for a cron note, so its open row keeps its own.
    expect(shown().sort()).toEqual(['8', '9'])
  })

  it('a retired row recedes in its header but keeps its notice at full contrast, in both variants', () => {
    const store = createTestStore({ notifications: { items: [{ ...approval, acked: true }], retiredApprovals: { '1': true } } as RootState['notifications'] })
    for (const variant of ['panel', 'mac'] as const) {
      const { unmount } = renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant={variant} />, { store })
      const notice = screen.getByTestId('notif-approval-retired')
      // No ancestor of the notice, up to and including the row, is dimmed.
      for (let el: HTMLElement | null = notice; el; el = el.parentElement) {
        expect(el.className).not.toMatch(/(^|\s)opacity-(50|55)(\s|$)/)
        if (el.hasAttribute('data-notif-row')) break
      }
      const row = notice.closest('[data-notif-row]') as HTMLElement
      expect(row.querySelector('.opacity-50, .opacity-55')).not.toBeNull()
      unmount()
    }
  })

  it('the row open in the detail panel leaves the retired sentence to that panel, in both variants', () => {
    const store = createTestStore({ notifications: { items: [{ ...approval, acked: true }], retiredApprovals: { '1': true } } as RootState['notifications'] })
    for (const variant of ['panel', 'mac'] as const) {
      const { unmount } = renderWithProviders(<NotificationFeed selectedTs="1" onSelect={() => {}} variant={variant} />, { store })
      expect(screen.queryByTestId('notif-approval-retired')).toBeNull()
      expect(screen.getByRole('button', { name: DISMISS() })).toBeInTheDocument()
      unmount()
    }
  })

  it('a retired row shows its close X without a hover and undimmed, in both variants', () => {
    const store = createTestStore({ notifications: { items: [{ ...approval, acked: true }], retiredApprovals: { '1': true } } as RootState['notifications'] })
    for (const variant of ['panel', 'mac'] as const) {
      const { unmount } = renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant={variant} />, { store })
      const close = screen.getByRole('button', { name: DISMISS() })
      expect(close.className).toContain('opacity-80')
      expect(close.className).not.toMatch(/(^|\s)opacity-0(\s|$)/)
      // The X is the row's only way out: no dimmed ancestor fades it further.
      for (let el = close.parentElement; el && !el.hasAttribute('data-notif-row'); el = el.parentElement) {
        expect(el.className).not.toMatch(/(^|\s)opacity-(50|55)(\s|$)/)
      }
      unmount()
    }
  })

  it('the bell popover decides the request its row names', async () => {
    const row: Notification = { ...approval, approval_instance: 'inst-pop', slot: 'chat-2' }
    const store = createTestStore({ notifications: { items: [row] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="mac" />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Reject$/ }))
    expect(mockResolveApproval).toHaveBeenCalledWith({ origin: 'coordinator', id: 'apr-1', slot: 'chat-2', instance: 'inst-pop' }, 'reject')
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
  })

  it.each(['panel', 'mac'] as const)('a row that names no request sends nothing and retires as refused (%s)', async variant => {
    // Written by an older build without the instance: no request can be named,
    // so no id is sent in its place.
    const { approval_instance: _none, ...row } = approval
    const store = createTestStore({ notifications: { items: [row] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant={variant} />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    expect(mockResolveApproval).not.toHaveBeenCalled()
    await waitFor(() => { expect(store.getState().notifications.retiredApprovals).toEqual({ '1': true }) })
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
  })

  it('a decide on the live row of a recurring id is bound to that row\'s own instance', async () => {
    // A press retired request A; the caller reused its id for request B.
    const rowA: Notification = { ...approval, approval_instance: 'inst-a' }
    const rowB: Notification = { ...approval, ts: '3', title: 'Tool approval: shell again', approval_instance: 'inst-b', slot: 'chat-1' }
    const store = createTestStore({
      notifications: { items: [rowA, rowB], retiredApprovals: { '1': true } } as RootState['notifications'],
    })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: /^Approve$/ }))
    expect(mockResolveApproval).toHaveBeenCalledTimes(1)
    expect(mockResolveApproval).toHaveBeenCalledWith({ origin: 'coordinator', id: 'apr-1', slot: 'chat-1', instance: 'inst-b' }, 'approve')
    await waitFor(() => { expect(store.getState().notifications.items.map(n => n.ts)).toEqual(['1']) })
    expect(store.getState().notifications.retiredApprovals).toEqual({ '1': true })
  })

  it('a row already retired when the feed mounts is dismissed with its close X', async () => {
    const store = createTestStore({ notifications: { items: [{ ...approval, acked: true }], retiredApprovals: { '1': true } } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
    expect(mockDeleteNotification).not.toHaveBeenCalled()
  })

  it('an ordinary row\'s close X still deletes it on the server', async () => {
    const note: Notification = { kind: 'cron', ts: '9', title: 'Job done', body: 'ok', acked: true }
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(mockDeleteNotification).toHaveBeenCalledWith('9') })
  })
  it('a stored note whose DELETE fails comes back with the reason, and its close X tries again', async () => {
    const note: Notification = { kind: 'cron', ts: '7', title: 'Job done', body: 'ok', acked: true }
    mockDeleteNotification.mockRejectedValueOnce(new Error('offline'))
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    const notice = await screen.findByTestId('notif-dismiss-failed')
    expect(notice).toHaveTextContent(i18nT('components.notifications.notificationFeed.dismiss_failed'))
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual(['7'])
    // The erosion is undone, so the row reads as it did.
    expect((screen.getByText(note.title).closest('[data-notif-row]') as HTMLElement).style.opacity).toBe('')
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
    expect(mockDeleteNotification).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['names no approval id', {}],
    ['carries an approval id from its meta', { approval_id: 'apr-from-meta' }],
  ])('a stored note of kind approval that %s is deleted on the server', async (_label, extra) => {
    // An app channel named `approval` stores its notes under that kind, and
    // its meta can name an approval id. Only a row this tab raised itself
    // carries `_local`, which the bus never lets a stored note carry.
    const note: Notification = { kind: 'approval', ts: '8', title: 'App approval note', body: 'x', acked: true, ...extra }
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />, { store })
    fireEvent.click(screen.getByRole('button', { name: DISMISS() }))
    await waitFor(() => { expect(mockDeleteNotification).toHaveBeenCalledWith('8') })
  })
})
