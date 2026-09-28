/**
 * Deep link: /notifications?note=<ts> opens one notification directly.
 *
 * External pushers (issue #2018: an ntfy bridge relaying the WS notification
 * stream) hard-code the `note` param in their Click URLs, so these tests pin
 * the page's side of that contract:
 *
 * - a valid id routes through the SAME select path as a tapped row (detail
 *   opens, auto-ack fires, feed row scrolls into view on desktop);
 * - the param is consumed with a history replace, so a re-render/back does not
 *   re-select and re-ack;
 * - the feed loads asynchronously, so an id that arrives before the feed must
 *   still resolve once the store fills;
 * - an unknown/expired id degrades to the plain page: nothing selected, no ack,
 *   no error surface.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { useLocation, useNavigate } from 'react-router-dom'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationsPage, { NOTE_DEEP_LINK_PARAM } from '../pages/NotificationsPage'
import { addNotification } from '../store/notificationsSlice'
import type { RootState } from '../store'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    taskRunToChat: vi.fn().mockResolvedValue({}),
    resolveApproval: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))

// happy-dom shims
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver
// The test DOM does not scroll; the spy is how the desktop scroll-into-view
// behavior is observable at all under the test environment.
const scrollSpy = vi.fn()
Element.prototype.scrollIntoView = scrollSpy

const mkN = (ts: string, title: string, acked = false): Notification => ({
  kind: 'cron', ts, title, body: `body for ${title}`, acked,
})

function stateWith(notifs: Notification[]): Partial<RootState> {
  return { notifications: { items: notifs } as RootState['notifications'] }
}

/** Renders the live router search string plus a back-navigation probe so
 *  tests can assert both consumption AND that it used a history REPLACE:
 *  with replace the deep-link entry no longer exists, so going back is a
 *  no-op at history index 0; with a push, back would restore `?note=`. */
function LocationProbe() {
  const loc = useLocation()
  const navigate = useNavigate()
  return (
    <div>
      <div data-testid="location-search">{loc.search}</div>
      <button data-testid="history-back" onClick={() => navigate(-1)}>back</button>
    </div>
  )
}

beforeEach(() => {
  localStorage.clear()
  scrollSpy.mockClear()
})

describe('NotificationsPage deep link (?note=<ts>)', () => {
  it('selects, acks, and scrolls the matching notification into view', async () => {
    const target = mkN('2026-05-29T10:01:00Z', 'Deploy Finished')
    const store = createTestStore(stateWith([
      mkN('2026-05-29T10:00:00Z', 'Cron Result', true),
      target,
    ]))
    renderWithProviders(<NotificationsPage />, {
      store,
      route: `/notifications?${NOTE_DEEP_LINK_PARAM}=${encodeURIComponent(target.ts)}`,
    })

    // Detail panel open: the empty-state placeholder is gone and the title
    // renders twice (feed row + detail header).
    await waitFor(() => {
      expect(screen.queryByText('Select a notification')).not.toBeInTheDocument()
      expect(screen.getAllByText('Deploy Finished').length).toBeGreaterThan(1)
    })
    // Auto-ack via the shared select path (optimistic store flip).
    expect(store.getState().notifications.items.find(n => n.ts === target.ts)?.acked).toBe(true)
    // Desktop: feed row scrolled into view.
    expect(scrollSpy).toHaveBeenCalled()
  })

  it('consumes the param (history replace) so a re-render does not re-select', async () => {
    const target = mkN('2026-05-29T10:01:00Z', 'Deploy Finished')
    const store = createTestStore(stateWith([target]))
    renderWithProviders(<><NotificationsPage /><LocationProbe /></>, {
      store,
      route: `/notifications?${NOTE_DEEP_LINK_PARAM}=${encodeURIComponent(target.ts)}`,
    })

    await waitFor(() => {
      expect(screen.getByTestId('location-search').textContent).toBe('')
    })
    // Close the detail; the consumed param must not re-select it.
    const back = await screen.findByRole('button', { name: /close/i })
    act(() => { back.click() })
    await waitFor(() => {
      expect(screen.getByText('Select a notification')).toBeInTheDocument()
    })
    // REPLACE (not push): the deep-link history entry is gone, so navigating
    // back must neither restore ?note= nor re-select/re-ack. A push-based
    // consume would fail here by returning to the param'd entry.
    act(() => { screen.getByTestId('history-back').click() })
    await waitFor(() => {
      expect(screen.getByTestId('location-search').textContent).toBe('')
      expect(screen.getByText('Select a notification')).toBeInTheDocument()
    })
  })

  it('resolves a deep link that arrives before the feed has loaded', async () => {
    const target = mkN('2026-05-29T10:01:00Z', 'Late Arrival')
    const store = createTestStore(stateWith([]))
    renderWithProviders(<NotificationsPage />, {
      store,
      route: `/notifications?${NOTE_DEEP_LINK_PARAM}=${encodeURIComponent(target.ts)}`,
    })

    // Feed still empty: nothing selected yet, page renders plainly.
    expect(screen.getByText('Select a notification')).toBeInTheDocument()

    // The slow fetch (or a WS row) delivers the note; the pending deep link
    // must resolve now rather than having been dropped at first render.
    act(() => { store.dispatch(addNotification(target)) })
    await waitFor(() => {
      expect(screen.queryByText('Select a notification')).not.toBeInTheDocument()
      expect(screen.getAllByText('Late Arrival').length).toBeGreaterThan(1)
    })
    expect(store.getState().notifications.items.find(n => n.ts === target.ts)?.acked).toBe(true)
  })

  it('an explicit tap disarms a pending deep link (user intent wins)', async () => {
    const rowA = mkN('2026-05-29T10:00:00Z', 'Tapped By User', true)
    const target = mkN('2026-05-29T10:01:00Z', 'Late Target')
    const store = createTestStore(stateWith([rowA]))
    renderWithProviders(<NotificationsPage />, {
      store,
      route: `/notifications?${NOTE_DEEP_LINK_PARAM}=${encodeURIComponent(target.ts)}`,
    })

    // User taps a row while the deep-link target is still unmatched.
    act(() => { screen.getByText('Tapped By User').click() })
    await waitFor(() => {
      expect(screen.getAllByText('Tapped By User').length).toBeGreaterThan(1)
    })

    // The target now arrives; it must NOT steal the selection or auto-ack.
    act(() => { store.dispatch(addNotification(target)) })
    await waitFor(() => {
      expect(screen.getByText('Late Target')).toBeInTheDocument()
    })
    expect(screen.getAllByText('Tapped By User').length).toBeGreaterThan(1)
    expect(store.getState().notifications.items.find(n => n.ts === target.ts)?.acked).toBe(false)
  })

  it('expands a collapsed group_key stack hiding the deep-linked note', async () => {
    // Two same-day notes share a group_key: the feed collapses them to the
    // newest head, so the older target renders no row until expanded.
    const head = { ...mkN('2026-05-29T10:05:00Z', 'Stack Head', true), group_key: 'ci' }
    const target = { ...mkN('2026-05-29T10:01:00Z', 'Stacked Target'), group_key: 'ci' }
    const store = createTestStore(stateWith([target, head]))
    renderWithProviders(<NotificationsPage />, {
      store,
      route: `/notifications?${NOTE_DEEP_LINK_PARAM}=${encodeURIComponent(target.ts)}`,
    })

    // Selection + ack happen regardless of stacking (page-level resolve)…
    await waitFor(() => {
      expect(store.getState().notifications.items.find(n => n.ts === target.ts)?.acked).toBe(true)
    })
    // …and the feed expands the stack so the target's row exists and scrolls.
    await waitFor(() => {
      expect(screen.getAllByText('Stacked Target').length).toBeGreaterThan(1)
      expect(scrollSpy).toHaveBeenCalled()
    })
  })

  it('degrades quietly to the plain page for an unknown/expired id', async () => {
    const bystander = mkN('2026-05-29T10:00:00Z', 'Cron Result')
    const store = createTestStore(stateWith([bystander]))
    renderWithProviders(<><NotificationsPage /><LocationProbe /></>, {
      store,
      route: `/notifications?${NOTE_DEEP_LINK_PARAM}=does-not-exist`,
    })

    // Param consumed even though nothing matched.
    await waitFor(() => {
      expect(screen.getByTestId('location-search').textContent).toBe('')
    })
    // Plain page: feed rendered, nothing selected, no ack fired, no scroll.
    // The bystander is seeded UNACKED so the assertion can actually fail if a
    // regression acks something on an unknown id.
    expect(screen.getByText('Cron Result')).toBeInTheDocument()
    expect(screen.getByText('Select a notification')).toBeInTheDocument()
    expect(store.getState().notifications.items.find(n => n.ts === bystander.ts)?.acked).toBe(false)
    expect(scrollSpy).not.toHaveBeenCalled()
  })
})
