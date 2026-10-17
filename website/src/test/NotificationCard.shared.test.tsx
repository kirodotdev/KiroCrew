/**
 * The bell popover's mac rows and the in-app banner must render the SAME
 * `NotificationCard` — one body, one material (the `Glass` pane the composer
 * dock wears), with nothing on the card saying which surface it is on. A
 * second look-alike rendering of a note, or a card-shaped div carrying its own
 * tint/blur/border classes, is the regression this file exists to catch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { createRef } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import NotificationBanner from '../components/notifications/NotificationBanner'
import NotificationCard from '../components/notifications/NotificationCard'
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
  it('the bell popover (variant="mac") renders every row through the card', () => {
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    const { container } = renderWithProviders(<NotificationFeed variant="mac" selectedTs={null} onSelect={() => {}} />, { store })
    const cards = cardsIn(container)
    expect(cards).toHaveLength(1)
    expect(cards[0].getAttribute('data-ts')).toBe(note.ts)
    // The title lives INSIDE the card, nowhere else on the surface.
    const title = screen.getByText('Shared card note')
    expect(cards[0].contains(title)).toBe(true)
    expect(screen.getByText('Open run').closest('[data-notification-card]')).toBe(cards[0])
  })

  it('the banner renders its top card through the same component', () => {
    const bellRef = createRef<HTMLButtonElement>()
    const { container } = renderWithProviders(
      <><button ref={bellRef}>bell</button><NotificationBanner bellRef={bellRef} popoverOpen={false} onOpenNote={() => {}} /></>,
      { route: '/settings' },
    )
    act(() => { dispatchLiveNotification(note) })
    const cards = cardsIn(container)
    expect(cards).toHaveLength(1)
    expect(cards[0].contains(screen.getByText('Shared card note'))).toBe(true)
    expect(screen.getByText('Open run').closest('[data-notification-card]')).toBe(cards[0])
    expect(cards[0].classList.contains('liquid-glass')).toBe(true)
  })

  it('the card is a Glass pane and carries no material of its own', () => {
    const { container } = renderWithProviders(<NotificationCard n={note} onOpen={() => {}} openLabel="open" />)
    const card = cardsIn(container)[0]
    expect(card.classList.contains('liquid-glass')).toBe(true)
    expect(card.classList.contains('glass-shadow')).toBe(true)
    // The index.css solidifying hook the sheet-dismiss predicate also keys on.
    expect(card.classList.contains('notif-material')).toBe(true)
    // No hand-rolled tint / blur / border: the pane's layers are the material.
    expect(card.className).not.toMatch(/\bbg-\[|backdrop-blur|\bborder\b|shadow-(md|lg)/)
    expect(card.querySelectorAll(':scope > [data-liquid-glass-layer]').length).toBeGreaterThan(0)
  })

  it('state is a tint step on the pane, never a border or an opacity', () => {
    const { container: a } = renderWithProviders(<NotificationCard n={note} active onOpen={() => {}} openLabel="open" />)
    expect(cardsIn(a)[0].classList.contains('glass-accent')).toBe(true)
    expect(cardsIn(a)[0].className).not.toMatch(/border-accent|bg-accent-subtle/)
    const { container: b } = renderWithProviders(<NotificationCard n={note} onOpen={() => {}} openLabel="open" />)
    expect(cardsIn(b)[0].classList.contains('glass-hover')).toBe(true)
    const { container: c } = renderWithProviders(<NotificationCard n={note} muted onOpen={() => {}} openLabel="open" />)
    expect(cardsIn(c)[0].classList.contains('glass-faded')).toBe(true)
    expect(cardsIn(c)[0].classList.contains('glass-hover')).toBe(false)
    expect(cardsIn(c)[0].className).not.toMatch(/border-dashed/)
    // Never `opacity` on a glass host: it would make the host a backdrop root
    // and void the pane's own blur.
    for (const el of [a, b, c]) expect(cardsIn(el)[0].className).not.toMatch(/\bopacity-\d/)
  })

  it('a deck card and the non-mac page list are not extra renderings of the body', () => {
    // The page (non-mac) list variant is a different, denser layout by design
    // and must NOT claim to be the shared card.
    const store = createTestStore({ notifications: { items: [note] } as RootState['notifications'] })
    const { container } = renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} />, { store })
    expect(cardsIn(container)).toHaveLength(0)
    expect(screen.getByText('Shared card note')).toBeTruthy()
  })

  it('the card carries no surface marker: nothing on it says popover or banner', () => {
    const { container } = renderWithProviders(<NotificationCard n={note} onOpen={() => {}} openLabel="open" />)
    const card = cardsIn(container)[0]
    expect(card.hasAttribute('data-elevation')).toBe(false)
    expect(card.className).not.toMatch(/popover|banner/)
  })
})
