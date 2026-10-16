/**
 * The detail panel's Approve/Reject read the same store marks as the feed.
 * A terminal refusal (the approval expired or was already decided elsewhere)
 * RETIRES the row: both buttons are withdrawn and an error notice replaces
 * them; the row leaves through the feed row's close X. A decision that lands
 * removes the row and closes the panel. An approval row is this tab's own copy
 * (the server keeps no approval note), so neither sends a DELETE.
 * Any other decision failure keeps the buttons, because the approval may
 * still be pending, unless a frame ended the request while the press was in
 * flight: the row then retires and shows the failure until dismissed.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, act, within, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders, createTestStore } from './helpers'
import { ThemeBootProbe, themeBootSettled } from './themeBootSettled'
import type { RootState } from '../store'
import { approvalDecisionSettled, clearAllNotifications, endApprovalRow, fetchNotifications } from '../store/notificationsSlice'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import NotificationFeed from '../components/notifications/NotificationFeed'
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
  body: 'gh issue view 14704', approval_id: 'apr-gone', acked: true, _local: true,
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
  it('a decision that lands removes the row and closes the panel', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue({})
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(api.resolveApproval).toHaveBeenCalledWith('apr-gone', 'reject')
    await waitFor(() => { expect(onClose).toHaveBeenCalledTimes(1) })
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
    expect(store.getState().notifications.retiredApprovals ?? {}).toEqual({})
    // The row was this tab's own copy: no request, so nothing can fail.
    expect(api.deleteNotification).not.toHaveBeenCalled()
  })
  it('a 404 retires the row: buttons withdrawn, error notice, no DELETE, panel stays open', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(notFound())
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

  it('a decision on a stored note whose DELETE fails keeps the panel open with a neutral recorded notice and no buttons', async () => {
    vi.mocked(api.resolveApproval).mockResolvedValue({})
    vi.mocked(api.deleteNotification).mockRejectedValueOnce(new Error('offline'))
    const stored: Notification = { ...approvalNote, _local: undefined }
    const store = createTestStore({ notifications: { items: [stored] } as RootState['notifications'] })
    const onClose = vi.fn()
    renderWithProviders(<NotificationDetailPanel n={stored} onClose={onClose} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    const recorded = await screen.findByTestId('notif-decision-recorded')
    // The decision landed, so that part leads as a polite neutral status. The
    // clean-up DELETE that failed after it is an error, so it renders beneath
    // through the shared ErrorNotice (role="alert").
    expect(recorded).toHaveAttribute('role', 'status')
    expect(recorded).toHaveAttribute('data-tone', 'info')
    expect(recorded).toHaveTextContent(i18nT('components.notifications.notificationFeed.decision_recorded'))
    expect(recorded).not.toHaveTextContent(i18nT('components.notifications.notificationFeed.decided_clear_failed'))
    const failed = screen.getByTestId('notif-dismiss-failed')
    expect(failed).toHaveAttribute('role', 'alert')
    expect(failed).toHaveTextContent(i18nT('components.notifications.notificationFeed.decided_clear_failed'))
    expect(screen.getAllByRole('alert')).toEqual([failed])
    // Close stays a plain Close; the destructive Dismiss sits by the notice.
    expect(screen.getByTestId('notif-detail-close')).toHaveTextContent(i18nT('components.notifications.notificationDetailPanel.close'))
    expect(screen.getByTestId('notif-detail-dismiss')).toHaveTextContent(i18nT('components.notifications.notificationFeed.dismiss_notification'))
    // Only Close carries the X glyph; Dismiss is text, so the two read apart.
    expect(screen.getByTestId('notif-detail-dismiss').querySelector('svg.lucide-x')).toBeNull()
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Reject/ })).toBeNull()
    expect(screen.queryByTestId('notif-approval-retired')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([stored.ts])
  })
  it('an expiry frame during the decision keeps the panel row and shows the refusal', async () => {
    let reject: (e: unknown) => void = () => {}
    vi.mocked(api.resolveApproval).mockReturnValueOnce(new Promise((_res, rej) => { reject = rej }))
    const { store, onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    act(() => { store.dispatch(endApprovalRow(approvalNote.ts)) })
    await act(async () => { reject(notFound()) })
    expect(await screen.findByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([approvalNote.ts])
    expect(onClose).not.toHaveBeenCalled()
  })
  for (const [label, failure] of [
    ['a 503', () => new ApiError(503, 'gateway busy, try again')],
    ['a transport rejection', () => new TypeError('Failed to fetch')],
  ] as const) {
    it(`${label} after an expiry frame during the decision keeps the panel row and shows the failure until dismissed`, async () => {
      let reject: (e: unknown) => void = () => {}
      vi.mocked(api.resolveApproval).mockReturnValueOnce(new Promise((_res, rej) => { reject = rej }))
      const { store, onClose } = renderPanel()
      await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
      act(() => { store.dispatch(endApprovalRow(approvalNote.ts)) })
      await act(async () => { reject(failure()) })
      const notice = await screen.findByTestId('notif-approval-refusal')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice).toHaveTextContent(i18nT('components.approvalCard.decision_failed'))
      expect(screen.getByTestId('notif-approval-retired')).toHaveTextContent(i18nT('components.approvalCard.approval_no_longer_pending'))
      // The failure leads, then "no longer pending", as on the feed row.
      expect(notice.compareDocumentPosition(screen.getByTestId('notif-approval-retired')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
      expect(screen.queryByRole('button', { name: /Reject/ })).toBeNull()
      expect(store.getState().notifications.items.map(n => n.ts)).toEqual([approvalNote.ts])
      expect(onClose).not.toHaveBeenCalled()
      await userEvent.click(screen.getByTestId('notif-detail-dismiss'))
      await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
    })
  }
  it('the retired row\'s Dismiss, next to its notice, takes the row out of the feed', async () => {
    const { store, onClose } = renderPanel()
    act(() => { store.dispatch(approvalDecisionSettled({ ts: approvalNote.ts, outcome: 'refused' })) })
    await userEvent.click(screen.getByTestId('notif-detail-dismiss'))
    expect(onClose).toHaveBeenCalledTimes(1)
    await waitFor(() => { expect(store.getState().notifications.items).toEqual([]) })
  })
  it('the retired row\'s Close only closes: it stays Close and leaves the row in the feed', async () => {
    const { store, onClose } = renderPanel()
    act(() => { store.dispatch(approvalDecisionSettled({ ts: approvalNote.ts, outcome: 'refused' })) })
    const close = screen.getByTestId('notif-detail-close')
    expect(close).toHaveTextContent(i18nT('components.notifications.notificationDetailPanel.close'))
    await userEvent.click(close)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(api.deleteNotification).not.toHaveBeenCalled()
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([approvalNote.ts])
  })
  it('Close on a live row leaves the row in the feed', async () => {
    const { store, onClose } = renderPanel()
    expect(screen.getByTestId('notif-detail-close')).toHaveTextContent(i18nT('components.notifications.notificationDetailPanel.close'))
    await userEvent.click(screen.getByTestId('notif-detail-close'))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([approvalNote.ts])
  })

  it('a 404 moves keyboard focus to the panel\'s Close once the pressed button is withdrawn', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(notFound())
    const { store } = renderPanel()
    screen.getByRole('button', { name: /Approve/ }).focus()
    await userEvent.keyboard('{Enter}')
    await screen.findByTestId('notif-approval-retired')
    await waitFor(() => { expect(document.activeElement).toBe(screen.getByTestId('notif-detail-close')) })
    // Focus lands on the non-destructive Close, never on the Dismiss: Enter
    // there closes the panel and keeps the row.
    expect(document.activeElement).not.toBe(screen.getByTestId('notif-detail-dismiss'))
    await userEvent.keyboard('{Enter}')
    expect(store.getState().notifications.items.map(n => n.ts)).toEqual([approvalNote.ts])
    expect(api.deleteNotification).not.toHaveBeenCalled()
  })

  it('a 404 does not pull focus away from a reader who has moved on', async () => {
    let refuse: (e: unknown) => void = () => {}
    vi.mocked(api.resolveApproval).mockImplementationOnce(() => new Promise((_, rej) => { refuse = rej }))
    const store = createTestStore({ notifications: { items: [approvalNote] } as RootState['notifications'] })
    renderWithProviders(<><input aria-label="Search" data-testid="search-elsewhere" /><NotificationDetailPanel n={approvalNote} onClose={vi.fn()} /></>, { store })
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    // The reader has moved on to a field outside the panel.
    const elsewhere = screen.getByTestId('search-elsewhere')
    elsewhere.focus()
    await act(async () => { refuse(notFound()) })
    await screen.findByTestId('notif-approval-retired')
    expect(document.activeElement).toBe(elsewhere)
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
    vi.mocked(api.resolveApproval).mockRejectedValue(new ApiError(400, 'invalid approval target'))
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

  it.each([503, 502])('a %i keeps the buttons and says it in plain words, never the layer\'s own text', async (status) => {
    vi.mocked(api.resolveApproval).mockRejectedValue(new ApiError(status, 'upstream proxy overloaded'))
    const { onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    const notice = await screen.findByTestId('notif-approval-refusal')
    expect(notice).toHaveTextContent(i18nT('components.notifications.notificationFeed.decision_failed_retry'))
    expect(notice.textContent).not.toContain('upstream proxy overloaded')
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('a transport failure with no response keeps the buttons and says the decision may not have landed', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(new TypeError('Failed to fetch'))
    const { onClose } = renderPanel()
    await userEvent.click(screen.getByRole('button', { name: /Reject/ }))
    expect(await screen.findByTestId('notif-approval-refusal')).toHaveTextContent(
      i18nT('components.notifications.notificationFeed.decision_failed_retry'),
    )
    expect(screen.getByRole('button', { name: /Approve/ })).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('a row that names its request decides with that target, and a refused stale target retires it', async () => {
    vi.mocked(api.resolveApproval).mockRejectedValue(notFound())
    const bound: Notification = { ...approvalNote, slot: 'slot-a', approval_instance: 'inst-a' }
    const store = createTestStore({ notifications: { items: [bound] } as RootState['notifications'] })
    renderWithProviders(<NotificationDetailPanel n={bound} onClose={() => {}} />, { store })
    await userEvent.click(screen.getByRole('button', { name: /Approve/ }))
    // The server refuses a stale instance instead of settling the request that
    // took the id over.
    expect(api.resolveApproval).toHaveBeenCalledWith('apr-gone', 'approve', { origin: 'coordinator', slot: 'slot-a', instance: 'inst-a' })
    expect(await screen.findByTestId('notif-approval-retired')).toBeInTheDocument()
  })

  it.each([
    ['a reconnect snapshot', fetchNotifications.fulfilled({ items: [], seq: 0, ackSeq: 0 }, '', undefined)],
    ['a clear-all', clearAllNotifications()],
  ])('%s that omits a deciding row keeps it, so a late 404 still renders the refusal', async (_label, replace) => {
    let refuse: (e: unknown) => void = () => {}
    vi.mocked(api.resolveApproval).mockReturnValue(new Promise((_res, rej) => { refuse = rej }))
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

  it('an approval row whose plain DELETE failed says so as an alert and never claims a recorded decision', () => {
    const store = createTestStore({ notifications: { items: [approvalNote], dismissFailed: { [approvalNote.ts]: 'dismiss' } } as RootState['notifications'] })
    renderWithProviders(<NotificationDetailPanel n={approvalNote} onClose={vi.fn()} />, { store })
    const failed = screen.getByTestId('notif-dismiss-failed')
    expect(failed).toHaveAttribute('role', 'alert')
    expect(failed).toHaveTextContent(i18nT('components.notifications.notificationFeed.dismiss_failed'))
    // A plain failure stays inline: the bare <span>, not the boxed block.
    expect(failed.tagName).toBe('SPAN')
    expect(screen.queryByTestId('notif-decision-recorded')).toBeNull()
  })

  it('a decided row\'s failed clean-up is boxed like the recorded status above it', () => {
    const stored: Notification = { ...approvalNote, _local: undefined }
    const store = createTestStore({ notifications: { items: [stored], dismissFailed: { [stored.ts]: 'decided' } } as RootState['notifications'] })
    renderWithProviders(<NotificationDetailPanel n={stored} onClose={vi.fn()} />, { store })
    // The block variant is the boxed <div>; the inline one is a bare <span>.
    expect(screen.getByTestId('notif-dismiss-failed').tagName).toBe('DIV')
  })

  // A stored approval that a 404 retired, open in the panel, then dismissed
  // through its feed close X: the feed leaves the open row's failed DELETE to
  // the panel, so the panel must say it, under "no longer pending", on the
  // page's split (panel feed) and in the bell sheet (mac feed) alike.
  describe.each([
    ['a 503', () => new ApiError(503, 'gateway busy, try again')],
    ['a transport rejection', () => new TypeError('Failed to fetch')],
  ] as const)('a retired row open in the panel whose Dismiss fails with %s', (_label, failure) => {
    it.each(['panel', 'mac'] as const)('says so as an alert under "no longer pending" (%s feed)', async variant => {
      const stored: Notification = { ...approvalNote, _local: undefined }
      vi.mocked(api.deleteNotification).mockRejectedValue(failure())
      const store = createTestStore({ notifications: { items: [stored], retiredApprovals: { [stored.ts]: true } } as RootState['notifications'] })
      renderWithProviders(
        <div data-notif-scope>
          <NotificationFeed selectedTs={stored.ts} onSelect={() => {}} variant={variant} />
          <NotificationDetailPanel n={stored} onClose={vi.fn()} />
        </div>,
        { store },
      )
      const list = screen.getByTestId('notification-feed-list')
      await userEvent.click(within(list).getByRole('button', { name: i18nT('components.notifications.notificationFeed.dismiss_notification') }))
      await waitFor(() => { expect(api.deleteNotification).toHaveBeenCalledWith(stored.ts) })
      const failed = await screen.findByTestId('notif-dismiss-failed')
      expect(failed).toHaveAttribute('role', 'alert')
      expect(failed).toHaveTextContent(i18nT('components.notifications.notificationFeed.dismiss_failed'))
      expect(failed.tagName).toBe('SPAN')
      // Once, in the panel, beneath the retired sentence.
      expect(screen.getAllByTestId('notif-dismiss-failed')).toHaveLength(1)
      expect(within(list).queryByTestId('notif-dismiss-failed')).toBeNull()
      const retired = screen.getByTestId('notif-approval-retired')
      expect(retired.compareDocumentPosition(failed) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(screen.queryByTestId('notif-decision-recorded')).toBeNull()
      expect(store.getState().notifications.items.map(n => n.ts)).toEqual([stored.ts])
    })
  })

  it('a retired row never claims a recorded decision for its failed DELETE, in the panel and both feeds', () => {
    const stored: Notification = { ...approvalNote, _local: undefined }
    const store = createTestStore({ notifications: {
      items: [stored], retiredApprovals: { [stored.ts]: true }, dismissFailed: { [stored.ts]: 'decided' },
    } as RootState['notifications'] })
    renderWithProviders(
      <>
        <NotificationDetailPanel n={stored} onClose={vi.fn()} />
        <NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />
        <NotificationFeed selectedTs={null} onSelect={() => {}} variant="mac" />
      </>,
      { store },
    )
    const failed = screen.getAllByTestId('notif-dismiss-failed')
    expect(failed).toHaveLength(3)
    for (const el of failed) {
      expect(el).toHaveTextContent(i18nT('components.notifications.notificationFeed.dismiss_failed'))
      expect(el.tagName).toBe('SPAN')
    }
    expect(screen.queryByTestId('notif-decision-recorded')).toBeNull()
  })

  // The REAL feed and panel under one host scope, as the bell sheet and the
  // page's split render them: the hand-off depends on the feed's own list
  // carrying data-notif-list and tabIndex -1.
  it('Dismiss hands keyboard focus to the real feed list in its scope, not to the page body', async () => {
    const store = createTestStore({ notifications: { items: [approvalNote], retiredApprovals: { [approvalNote.ts]: true } } as RootState['notifications'] })
    const onClose = vi.fn()
    renderWithProviders(
      <div data-notif-scope>
        <NotificationFeed selectedTs={approvalNote.ts} onSelect={() => {}} variant="panel" />
        <NotificationDetailPanel n={approvalNote} onClose={onClose} />
      </div>,
      { store },
    )
    screen.getByTestId('notif-detail-dismiss').focus()
    await userEvent.keyboard('{Enter}')
    expect(onClose).toHaveBeenCalledTimes(1)
    const list = screen.getByTestId('notification-feed-list')
    await waitFor(() => { expect(document.activeElement).toBe(list) })
    // Programmatically focusable only: never a Tab stop of its own.
    expect(list).toHaveAttribute('tabindex', '-1')
    // The list announces what it is when focus lands on it.
    expect(list).toHaveAttribute('role', 'group')
    expect(list).toHaveAccessibleName(i18nT('app.notifications'))
  })

  // Safari, and Firefox on macOS, never focus a <button> on a mouse click, so
  // focus can still be in the feed's search box when Dismiss is clicked. The
  // hand-off is a rescue for lost focus only, like the panel's own.
  it('a Dismiss click that never moved focus leaves the reader where they were', async () => {
    const store = createTestStore({ notifications: { items: [approvalNote], retiredApprovals: { [approvalNote.ts]: true } } as RootState['notifications'] })
    renderWithProviders(
      <div data-notif-scope>
        <input data-testid="reader-input" aria-label="Search" />
        <NotificationFeed selectedTs={approvalNote.ts} onSelect={() => {}} variant="panel" />
        <NotificationDetailPanel n={approvalNote} onClose={vi.fn()} />
      </div>,
      { store },
    )
    const input = screen.getByTestId('reader-input')
    input.focus()
    fireEvent.click(screen.getByTestId('notif-detail-dismiss'))
    await act(async () => { await new Promise<void>(r => requestAnimationFrame(() => r())) })
    await act(async () => { await new Promise<void>(r => requestAnimationFrame(() => r())) })
    expect(document.activeElement).toBe(input)
  })

  // The bell sheet is portalled after the page, so a document-order lookup
  // would pick the page's list sitting under the open sheet.
  it('with two hosts open, Dismiss focuses the list in the panel\'s own scope', async () => {
    const store = createTestStore({ notifications: { items: [approvalNote], retiredApprovals: { [approvalNote.ts]: true } } as RootState['notifications'] })
    renderWithProviders(
      <>
        <div data-notif-scope data-testid="page-scope">
          <NotificationFeed selectedTs={null} onSelect={() => {}} variant="panel" />
        </div>
        <div data-notif-scope data-testid="sheet-scope">
          <NotificationFeed selectedTs={approvalNote.ts} onSelect={() => {}} variant="panel" />
          <NotificationDetailPanel n={approvalNote} onClose={vi.fn()} />
        </div>
      </>,
      { store },
    )
    const sheetList = within(screen.getByTestId('sheet-scope')).getByTestId('notification-feed-list')
    screen.getByTestId('notif-detail-dismiss').focus()
    await userEvent.keyboard('{Enter}')
    await waitFor(() => { expect(document.activeElement).toBe(sheetList) })
  })
})
