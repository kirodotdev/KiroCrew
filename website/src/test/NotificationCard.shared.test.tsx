/**
 * The bell popover's mac rows and the in-app banner must render the SAME
 * `NotificationCard`, differing only in elevation material. A second
 * look-alike rendering of a note is the regression this file exists to catch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { createRef } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import NotificationBanner from '../components/notifications/NotificationBanner'
import NotificationCard, { CARD_MATERIAL } from '../components/notifications/NotificationCard'
import { dispatchLiveNotification } from '../hooks/notificationEvent'
import type { RootState } from '../store'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    updateNotificationChannelSettings: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const SKIP = new Set(['layout', 'layoutId', 'initial', 'animate', 'exit', 'transition', 'variants', 'custom'])
  const div = React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
    const clean: Record<string, unknown> = {}
    for (const k of Object.keys(props)) if (k !== 'children' && !SKIP.has(k)) clean[k] = props[k]
    return React.createElement('div', { ...clean, ref }, props.children)
  })
  return {
    motion: { div },
    AnimatePresence: ({ children }: { children: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const note: Notification = {
  kind: 'cron', ts: '2026-09-21T10:00:00.000Z', title: 'Shared card note', body: 'one body', acked: false,
  actions: [{ id: 'a', label: 'Open run', url: '/schedule' }],
}

beforeEach(() => { localStorage.clear(); vi.spyOn(document, 'hasFocus').mockReturnValue(true) })

const cardsIn = (root: ParentNode) => Array.from(root.querySelectorAll<HTMLElement>('[data-notification-card]'))

describe('NotificationCard is the one rendering for both mac surfaces', () => {
  it('the bell popover (variant="mac") renders every row through the card at popover elevation', () => {
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    const { container } = renderWithProviders(<NotificationFeed variant="mac" selectedTs={null} onSelect={() => {}} />, { store })
    const cards = cardsIn(container)
    expect(cards).toHaveLength(1)
    expect(cards[0].getAttribute('data-elevation')).toBe('popover')
    expect(cards[0].getAttribute('data-ts')).toBe(note.ts)
    // The title lives INSIDE the card, nowhere else on the surface.
    const title = screen.getByText('Shared card note')
    expect(cards[0].contains(title)).toBe(true)
    expect(screen.getByText('Open run').closest('[data-notification-card]')).toBe(cards[0])
  })

  it('the banner renders its top card through the same component at banner elevation', () => {
    const bellRef = createRef<HTMLButtonElement>()
    const { container } = renderWithProviders(
      <><button ref={bellRef}>bell</button><NotificationBanner bellRef={bellRef} popoverOpen={false} onOpenNote={() => {}} /></>,
      { route: '/settings' },
    )
    act(() => { dispatchLiveNotification(note) })
    const cards = cardsIn(container)
    expect(cards).toHaveLength(1)
    expect(cards[0].getAttribute('data-elevation')).toBe('banner')
    expect(cards[0].contains(screen.getByText('Shared card note'))).toBe(true)
    expect(screen.getByText('Open run').closest('[data-notification-card]')).toBe(cards[0])
    for (const cls of CARD_MATERIAL.banner.split(' ')) expect(cards[0].classList.contains(cls)).toBe(true)
  })

  it('a deck card and the non-mac page list are not extra renderings of the body', () => {
    // The page (non-mac) list variant is a different, denser layout by design
    // and must NOT claim to be the shared card.
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    const { container } = renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} />, { store })
    expect(cardsIn(container)).toHaveLength(0)
    expect(screen.getByText('Shared card note')).toBeTruthy()
  })

  it('only the material differs between elevations', () => {
    const { container: a } = renderWithProviders(<NotificationCard n={note} elevation="popover" onOpen={() => {}} openLabel="open" />)
    const { container: b } = renderWithProviders(<NotificationCard n={note} elevation="banner" onOpen={() => {}} openLabel="open" />)
    const strip = (el: HTMLElement) => {
      const clone = el.cloneNode(true) as HTMLElement
      clone.removeAttribute('class'); clone.removeAttribute('data-elevation')
      return clone.innerHTML
    }
    expect(strip(cardsIn(a)[0])).toBe(strip(cardsIn(b)[0]))
    expect(cardsIn(a)[0].className).not.toBe(cardsIn(b)[0].className)
  })
})
