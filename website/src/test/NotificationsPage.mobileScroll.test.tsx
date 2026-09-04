/**
 * /notifications scroll contract, per viewport.
 *
 * Desktop height-locks the primary/detail split (`overflow-hidden` container,
 * `flex-1 min-h-0` split) so feed and detail scroll as independent panes. On
 * mobile the split collapses to one column and the stat grid stacks several
 * rows tall, so the same lock pins the feed and detail to whatever sliver is
 * left under the grid — under real phone browser chrome that sliver is partly
 * or fully hidden, and nothing on the page answers a swipe. Mobile therefore
 * uses the standard page skeleton (`overflow-y-auto` container, natural-height
 * split) and the page scrolls as a whole.
 *
 * happy-dom does not do layout, so these tests pin the classes that select the
 * scroll behavior, the same way the touch-footer tests pin the hover-none
 * override.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationsPage from '../pages/NotificationsPage'
import type { RootState } from '../store'
import type { Notification } from '../types'

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({
  useIsMobile: () => mobile,
}))

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

const note: Notification = {
  kind: 'cron', ts: '2026-05-29T10:00:00Z', title: 'Cron Result', body: 'cron body text', acked: true,
}

function stateWith(notifs: Notification[]): Partial<RootState> {
  return { notifications: { items: notifs } as RootState['notifications'] }
}

/** The content container is the page skeleton's div directly after PageHeader. */
function contentContainer(): HTMLElement {
  const el = screen.getByTestId('page-header').nextElementSibling as HTMLElement
  expect(el).not.toBeNull()
  return el
}

/** The feed/detail split is the container's last child (after the stat grid). */
function splitRow(): HTMLElement {
  return contentContainer().lastElementChild as HTMLElement
}

beforeEach(() => {
  localStorage.clear()
  mobile = false
})

describe('NotificationsPage scroll containers', () => {
  it('desktop: height-locked container with a flex-1 min-h-0 split', () => {
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, { store })

    expect(contentContainer().className).toContain('overflow-hidden')
    expect(contentContainer().className).not.toContain('overflow-y-auto')
    expect(splitRow().className).toContain('flex-1')
    expect(splitRow().className).toContain('min-h-0')
  })

  it('mobile: the page scrolls as a whole, the split takes natural height', () => {
    mobile = true
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, { store })

    expect(contentContainer().className).toContain('overflow-y-auto')
    expect(contentContainer().className).not.toContain('overflow-hidden')
    expect(splitRow().className).not.toContain('flex-1')
    expect(splitRow().className).not.toContain('min-h-0')
  })

  it('mobile detail: natural-height card with a sticky Back row above it', () => {
    mobile = true
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, { store })

    fireEvent.click(screen.getByRole('button', { name: 'Open notification: Cron Result' }))

    // The Back row lives OUTSIDE the Card (.card-glow is overflow-hidden,
    // which disables sticky within) and sticks to the page scroll container
    // so a long body always keeps an exit in view.
    const backRow = screen.getByRole('button', { name: 'Back' }).parentElement as HTMLElement
    expect(backRow.className).toContain('sticky')
    expect(backRow.className).toContain('top-0')
    expect(backRow.closest('.card-glow')).toBeNull()

    // The detail card itself takes natural height so the body scrolls with
    // the page instead of inner-scrolling a clipped pane.
    const card = backRow.nextElementSibling as HTMLElement
    expect(card.className).toContain('card-glow')
    expect(card.className).not.toContain('h-full')
    expect(card.className).not.toContain('min-h-0')
  })

  it('mobile: opens the detail at the top and restores the feed offset on Back', () => {
    // Feed and detail share ONE scroll container on mobile; without explicit
    // management the feed's large scrollTop clamps to the shorter detail's
    // bottom (user lands at the end, Back off-screen) and the feed position
    // is lost on return. happy-dom does no layout but scrollTop is a plain
    // settable property, so the capture/reset/restore contract is assertable.
    mobile = true
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, { store })

    const container = contentContainer()
    container.scrollTop = 1234 // user is deep in the feed

    fireEvent.click(screen.getByRole('button', { name: 'Open notification: Cron Result' }))
    expect(container.scrollTop).toBe(0) // detail opens at the top

    container.scrollTop = 555 // user reads down the detail
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(container.scrollTop).toBe(1234) // feed position restored
  })

  it('desktop: selection does not touch the container scroll position', () => {
    // Desktop panes scroll independently; the mobile-only management must not
    // reach in and reset them.
    const store = createTestStore(stateWith([note]))
    renderWithProviders(<NotificationsPage />, { store })

    const container = contentContainer()
    container.scrollTop = 42
    fireEvent.click(screen.getByRole('button', { name: 'Open notification: Cron Result' }))
    expect(container.scrollTop).toBe(42)
  })
})
