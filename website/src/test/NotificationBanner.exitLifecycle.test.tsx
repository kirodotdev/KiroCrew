/**
 * The banner's hold re-derivation against the REAL framer-motion lifecycle.
 *
 * NotificationBanner.test.tsx mocks AnimatePresence to a Fragment, which
 * unmounts a dismissed card in the same commit that drops it from `pending`.
 * Production does not: AnimatePresence keeps a leaving card mounted until its
 * exit animation finishes, so a focused control inside it is still the active
 * element when the `[pending]` effect re-reads who holds the clock. Nothing is
 * mocked out of framer-motion here, so that exit phase is the real one.
 *
 * framer-motion's frameloop captures `requestAnimationFrame` when its module
 * is first evaluated, so the fake clock is installed in `vi.hoisted` -- before
 * any import -- and kept for the whole file. The exit animation and the
 * banner's own auto-hide `setTimeout` then advance on ONE timeline, and "the
 * exit has finished" is a deterministic step rather than a real-time wait.
 */
import { describe, it, expect, beforeEach, afterEach, afterAll, vi } from 'vitest'

vi.hoisted(() => {
  vi.useFakeTimers({
    toFake: ['setTimeout', 'clearTimeout', 'Date', 'performance', 'requestAnimationFrame', 'cancelAnimationFrame'],
  })
})

import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { createRef } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationBanner from '../components/notifications/NotificationBanner'
import { dispatchLiveNotification } from '../hooks/notificationEvent'
import { BANNER_AUTO_HIDE_MS } from '../hooks/notificationBanner'
import type { RootState } from '../store'
import type { Notification } from '../types'

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
  },
}))
let isMobileMock = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => isMobileMock }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

let seq = 0
const mkN = (over: Partial<Notification> = {}): Notification => ({
  kind: 'cron', ts: `2026-09-21T10:00:0${seq++}.000Z`, title: `Note ${seq}`, body: 'body text', acked: false, ...over,
})

function renderBanner() {
  const store = createTestStore({ notifications: { items: [] } as unknown as RootState['notifications'] })
  const bellRef = createRef<HTMLButtonElement>()
  return renderWithProviders(
    <>
      <button ref={bellRef}>bell</button>
      <NotificationBanner bellRef={bellRef} popoverOpen={false} onOpenNote={vi.fn()} />
    </>,
    { store, route: '/settings' },
  )
}

const arrive = (n: Notification) => act(() => { dispatchLiveNotification(n) })
const cards = () => screen.queryAllByTestId('notification-banner-card')
const pendingCount = () => Number(screen.getByTestId('notification-banner-region').getAttribute('data-banner-count'))
// Async: a leaving card is released through the exit animation's promise, so
// microtasks must drain between frames for the unmount to land.
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
// Comfortably past the longest exit variant (0.26s travel).
const EXIT_MS = 1000
// Let `ms` pass, then let any exit that started meanwhile play out. Two steps:
// a removal made inside one `act` only commits when that act ends, so its exit
// animation cannot start, let alone finish, within the same advance.
const settle = async (ms: number) => { await advance(ms); await advance(EXIT_MS) }

beforeEach(() => {
  localStorage.clear()
  seq = 0
  isMobileMock = false
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
})

// No clearAllTimers: it would drop the frame framer-motion's loop has already
// requested and strand that loop for every later test in the file.
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

afterAll(() => { vi.useRealTimers() })

/** Two default cards, focus on the top card's dismiss button. */
function focusedDeck() {
  renderBanner()
  arrive(mkN({ title: 'Older' }))
  arrive(mkN({ title: 'Newest' }))
  const x = screen.getByTestId('notification-banner-dismiss')
  act(() => { x.focus() })
  return { x, stack: cards()[0].parentElement! }
}

describe('NotificationBanner: holds across a real exit animation', () => {
  it('keeps the dismissed card mounted, and focused, through its exit', async () => {
    const { x } = focusedDeck()
    fireEvent.click(x)
    // One note is pending, yet two cards are in the DOM and focus is still in
    // the leaving one: the window the [pending] re-derivation runs in.
    expect(pendingCount()).toBe(1)
    expect(cards()).toHaveLength(2)
    expect(document.activeElement).toBe(x)
    await advance(EXIT_MS)
    expect(cards()).toHaveLength(1)
    expect(x.isConnected).toBe(false)
  })

  it('auto-hides the surviving card once a FOCUSED card has finished leaving', async () => {
    const { x } = focusedDeck()
    fireEvent.click(x)
    await advance(EXIT_MS)
    expect(cards()).toHaveLength(1)
    await settle(BANNER_AUTO_HIDE_MS * 2)
    expect(cards()).toHaveLength(0)
  })

  it('auto-hides the survivor after a mouse dismissal once the pointer leaves', async () => {
    // A mouse click focuses the button it lands on, so the pointer and the
    // focus both hold the clock; the card leaving releases the focus, and the
    // pointer leaving releases the rest.
    const { x, stack } = focusedDeck()
    fireEvent.pointerEnter(stack)
    fireEvent.click(x)
    await advance(EXIT_MS)
    fireEvent.pointerLeave(stack)
    await settle(BANNER_AUTO_HIDE_MS + 1)
    expect(cards()).toHaveLength(0)
  })

  it('releases focus left on a card an ARRIVAL pushed out of view (mobile)', async () => {
    // Mobile shows the newest card alone, so an arrival makes the focused one
    // leave while its note is still pending.
    isMobileMock = true
    renderBanner()
    arrive(mkN({ title: 'First' }))
    act(() => { screen.getByTestId('notification-banner-dismiss').focus() })
    arrive(mkN({ title: 'Second' }))
    await advance(EXIT_MS)
    expect(cards()).toHaveLength(1)
    await settle(BANNER_AUTO_HIDE_MS + 1)
    expect(cards()).toHaveLength(0)
  })

  it('does not take a hold for focus moved WITHIN a leaving card', async () => {
    const { x } = focusedDeck()
    const leaving = cards()[0]
    fireEvent.click(x)
    // Tab to another control of the card that is already on its way out.
    const other = [...leaving.querySelectorAll<HTMLElement>('button, [tabindex]')].find(el => el !== x)!
    act(() => { other.focus() })
    await settle(0)
    await settle(BANNER_AUTO_HIDE_MS + 1)
    expect(cards()).toHaveLength(0)
  })

  it('keeps a focus hold a SURVIVING card still owns', async () => {
    renderBanner()
    arrive(mkN({ title: 'Older' }))
    arrive(mkN({ title: 'Newest' }))
    fireEvent.click(screen.getByTestId('notification-banner-count'))
    const dismissals = screen.getAllByTestId('notification-banner-dismiss')
    expect(dismissals).toHaveLength(2)
    act(() => { dismissals[1].focus() })
    fireEvent.keyDown(document, { key: 'Escape' })
    await settle(0)
    await settle(BANNER_AUTO_HIDE_MS * 2)
    expect(cards()).toHaveLength(1)
    expect(document.activeElement).toBe(dismissals[1])
    // And a real blur out of the banner still releases it.
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    act(() => { outside.focus() })
    await settle(BANNER_AUTO_HIDE_MS + 1)
    expect(cards()).toHaveLength(0)
    outside.remove()
  })

  it('keeps the pointer hold while the pointer is still over the banner', async () => {
    const { x, stack } = focusedDeck()
    fireEvent.pointerEnter(stack)
    fireEvent.click(x)
    await settle(0)
    await settle(BANNER_AUTO_HIDE_MS * 2)
    expect(cards()).toHaveLength(1)
  })

  it('clears every hold on an emptied deck, so the next card runs its clock', async () => {
    renderBanner()
    arrive(mkN())
    const x = screen.getByTestId('notification-banner-dismiss')
    fireEvent.pointerEnter(cards()[0].parentElement!)
    act(() => { x.focus() })
    fireEvent.click(x)
    await advance(EXIT_MS)
    expect(cards()).toHaveLength(0)
    arrive(mkN())
    await settle(BANNER_AUTO_HIDE_MS + 1)
    expect(cards()).toHaveLength(0)
  })

  it('leaves a critical survivor up: the exit arms no clock for it', async () => {
    renderBanner()
    arrive(mkN({ priority: 'critical', title: 'Approve tool' }))
    arrive(mkN({ title: 'Newest' }))
    const x = screen.getAllByTestId('notification-banner-dismiss')[0]
    act(() => { x.focus() })
    fireEvent.click(x)
    await settle(0)
    await settle(BANNER_AUTO_HIDE_MS * 3)
    expect(cards()).toHaveLength(1)
    expect(cards()[0].getAttribute('data-priority')).toBe('critical')
  })
})
