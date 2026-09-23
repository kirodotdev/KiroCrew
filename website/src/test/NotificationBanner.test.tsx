/**
 * In-app notification banner: arrival, the suppression list, the shared
 * auto-hide timer, the deck, keyboard dismissal, and the reduced-motion exit.
 *
 * framer-motion is mocked to plain elements: the assertions here are about
 * WHICH cards exist and what their attributes say, not about interpolated
 * transforms. The exit geometry is unit-tested through the exported
 * `exitTarget` / `computeExitDelta` helpers instead, which is where the
 * reduced-motion "no travel" rule actually lives.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { createRef } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationBanner, { computeExitDelta, exitTarget } from '../components/notifications/NotificationBanner'
import { dispatchLiveNotification } from '../hooks/notificationEvent'
import { BANNER_AUTO_HIDE_MS, BANNER_ENABLED_KEY, saveBannerEnabled, shouldBannerNote, targetsCurrentView } from '../hooks/notificationBanner'
import type { RootState } from '../store'
import type { Notification } from '../types'

const mockAck = vi.fn().mockResolvedValue({})
vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: (...args: unknown[]) => mockAck(...args),
  },
}))

// The leave guard: `true` lets a navigation through, `false` is the user
// answering "stay" to an unsaved-draft prompt.
let mayLeaveMock = true
const mockNavigate = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => mockNavigate }
})
vi.mock('../components/NavigationLeaveGuard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/NavigationLeaveGuard')>()
  return {
    ...actual,
    useGuardedLeave: () => (perform: () => void | Promise<void>) => { if (mayLeaveMock) void perform() },
  }
})

let isMobileMock = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => isMobileMock }))

let reducedMotionMock = false
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set(['layout', 'layoutId', 'initial', 'animate', 'exit', 'transition', 'variants', 'custom'])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  return {
    motion: { div: make('div') },
    AnimatePresence: ({ children }: { children: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => reducedMotionMock,
  }
})

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

let seq = 0
const mkN = (over: Partial<Notification> = {}): Notification => ({
  kind: 'cron', ts: `2026-09-21T10:00:0${seq++}.000Z`, title: `Note ${seq}`, body: 'body text', acked: false, ...over,
})

function renderBanner(opts: { popoverOpen?: boolean; route?: string; activeSlot?: string | null; items?: Notification[] } = {}) {
  const store = createTestStore({
    notifications: { items: opts.items ?? [] } as RootState['notifications'],
    ...(opts.activeSlot !== undefined ? { chat: { activeSlot: opts.activeSlot } as RootState['chat'] } : {}),
  })
  const bellRef = createRef<HTMLButtonElement>()
  const onOpenNote = vi.fn()
  const utils = renderWithProviders(
    <>
      <button ref={bellRef}>bell</button>
      <NotificationBanner bellRef={bellRef} popoverOpen={opts.popoverOpen ?? false} onOpenNote={onOpenNote} />
    </>,
    { store, route: opts.route ?? '/settings' },
  )
  return { ...utils, store, onOpenNote }
}

const arrive = (n: Notification) => act(() => { dispatchLiveNotification(n) })
const cards = () => screen.queryAllByTestId('notification-banner-card')

beforeEach(() => {
  localStorage.clear()
  mockAck.mockReset().mockResolvedValue({})
  mockNavigate.mockReset()
  mayLeaveMock = true
  isMobileMock = false
  reducedMotionMock = false
  seq = 0
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('NotificationBanner: arrival', () => {
  it('renders a card for a live default-priority note, in a polite live region', () => {
    renderBanner()
    const region = screen.getByTestId('notification-banner-region')
    expect(region.getAttribute('role')).toBe('status')
    expect(cards()).toHaveLength(0)
    arrive(mkN({ title: 'Digest ready' }))
    expect(cards()).toHaveLength(1)
    expect(screen.getByText('Digest ready')).toBeTruthy()
    expect(region.getAttribute('aria-live')).toBe('polite')
  })

  it('never renders passive or silenced notes', () => {
    renderBanner()
    arrive(mkN({ priority: 'passive' }))
    arrive(mkN({ silenced: true, priority: 'passive' }))
    arrive(mkN({ silenced: true }))
    expect(cards()).toHaveLength(0)
  })

  it('does not banner a note added through the store (boot replay path)', () => {
    const { store } = renderBanner()
    act(() => {
      store.dispatch({ type: 'notifications/addNotification', payload: mkN({ title: 'From snapshot' }) })
    })
    expect(cards()).toHaveLength(0)
  })

  it('does not steal focus from the composer on arrival', () => {
    renderBanner()
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()
    arrive(mkN())
    expect(document.activeElement).toBe(input)
    input.remove()
  })

  it('switches to an alert region while a critical card is pending', () => {
    renderBanner()
    arrive(mkN({ priority: 'critical', kind: 'approval' }))
    const region = screen.getByTestId('notification-banner-region')
    expect(region.getAttribute('role')).toBe('alert')
    expect(cards()[0].getAttribute('data-priority')).toBe('critical')
  })
})

describe('NotificationBanner: suppression', () => {
  it('suppresses while the bell popover is open', () => {
    renderBanner({ popoverOpen: true })
    arrive(mkN())
    expect(cards()).toHaveLength(0)
  })

  it('suppresses on the /notifications page', () => {
    renderBanner({ route: '/notifications' })
    arrive(mkN())
    expect(cards()).toHaveLength(0)
  })

  it('suppresses when the setting is OFF, and honours a live flip', () => {
    localStorage.setItem(BANNER_ENABLED_KEY, '0')
    renderBanner()
    arrive(mkN())
    expect(cards()).toHaveLength(0)
    act(() => { saveBannerEnabled(true) })
    arrive(mkN())
    expect(cards()).toHaveLength(1)
    act(() => { saveBannerEnabled(false) })
    expect(cards()).toHaveLength(0)
  })

  it('suppresses a note about the chat on screen while the window is focused', () => {
    renderBanner({ route: '/chat/abc', activeSlot: 'chat-1' })
    arrive(mkN({ slot: 'chat-1' }))
    expect(cards()).toHaveLength(0)
    arrive(mkN({ slot: 'chat-2' }))
    expect(cards()).toHaveLength(1)
  })

  it('banners the active chat when the window is NOT focused', () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    renderBanner({ route: '/chat/abc', activeSlot: 'chat-1' })
    arrive(mkN({ slot: 'chat-1' }))
    expect(cards()).toHaveLength(1)
  })

  it('retires pending cards when the popover opens', () => {
    const { rerender, store } = renderBanner()
    arrive(mkN())
    expect(cards()).toHaveLength(1)
    const bellRef = createRef<HTMLButtonElement>()
    rerender(
      <>
        <button ref={bellRef}>bell</button>
        <NotificationBanner bellRef={bellRef} popoverOpen onOpenNote={() => {}} />
      </>,
    )
    void store
    expect(cards()).toHaveLength(0)
  })
})

describe('shouldBannerNote / targetsCurrentView (pure)', () => {
  const base = { enabled: true, popoverOpen: false, pathname: '/settings', activeSlot: null, windowFocused: true }
  it('refuses each suppression case independently', () => {
    const n = mkN()
    expect(shouldBannerNote(n, base)).toBe(true)
    expect(shouldBannerNote(n, { ...base, enabled: false })).toBe(false)
    expect(shouldBannerNote(n, { ...base, popoverOpen: true })).toBe(false)
    expect(shouldBannerNote(n, { ...base, pathname: '/notifications' })).toBe(false)
    expect(shouldBannerNote(mkN({ priority: 'passive' }), base)).toBe(false)
    expect(shouldBannerNote(mkN({ silenced: true }), base)).toBe(false)
  })
  it('treats a deep link to the current route as already on screen', () => {
    expect(targetsCurrentView(mkN({ url: '/schedule?job=1' }), { pathname: '/schedule', activeSlot: null, windowFocused: true })).toBe(true)
    expect(targetsCurrentView(mkN({ url: '/schedule' }), { pathname: '/schedule', activeSlot: null, windowFocused: false })).toBe(false)
    expect(targetsCurrentView(mkN({ url: '/schedule' }), { pathname: '/settings', activeSlot: null, windowFocused: true })).toBe(false)
  })
})

describe('NotificationBanner: auto-hide', () => {
  it('removes a default card after the delay without acking it, and pauses while hovered', () => {
    const n = mkN()
    const { store } = renderBanner({ items: [n] })
    vi.useFakeTimers()
    arrive(n)
    expect(cards()).toHaveLength(1)
    // Hover pauses the clock: well past the delay, the card is still there.
    const stack = cards()[0].parentElement!
    fireEvent.pointerEnter(stack)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS * 2) })
    expect(cards()).toHaveLength(1)
    fireEvent.pointerLeave(stack)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS - 1) })
    expect(cards()).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(2) })
    expect(cards()).toHaveLength(0)
    // Auto-hide is not a read: the bell's unread dot must stay lit.
    expect(store.getState().notifications.items[0].acked).toBe(false)
  })

  it('keeps a critical card past the delay and lets it be dismissed', () => {
    renderBanner()
    vi.useFakeTimers()
    arrive(mkN({ priority: 'critical', title: 'Approve tool' }))
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS * 3) })
    expect(cards()).toHaveLength(1)
    fireEvent.click(screen.getByTestId('notification-banner-dismiss'))
    expect(cards()).toHaveLength(0)
  })

  it('shares one timer across default cards, restarted by each arrival', () => {
    renderBanner()
    vi.useFakeTimers()
    arrive(mkN())
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS - 500) })
    arrive(mkN())
    act(() => { vi.advanceTimersByTime(1000) })
    // The first card would have hidden by now on its own clock; the second
    // arrival restarted the shared one.
    expect(cards()).toHaveLength(2)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS) })
    expect(cards()).toHaveLength(0)
  })

  it('keeps the card while focus is inside it after the pointer leaves', () => {
    const n = mkN()
    renderBanner({ items: [n] })
    vi.useFakeTimers()
    arrive(n)
    const stack = cards()[0].parentElement!
    const x = screen.getByTestId('notification-banner-dismiss')
    // Keyboard first, pointer second: two independent holds on the same card.
    act(() => { x.focus() })
    fireEvent.pointerEnter(stack)
    // The pointer goes; focus stays. The hold the keyboard took is still on.
    fireEvent.pointerLeave(stack)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS * 2) })
    expect(cards()).toHaveLength(1)
    expect(document.activeElement).toBe(x)
  })

  it('keeps the card while the pointer is over it after focus leaves', () => {
    const n = mkN()
    renderBanner({ items: [n] })
    vi.useFakeTimers()
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    arrive(n)
    const stack = cards()[0].parentElement!
    fireEvent.pointerEnter(stack)
    act(() => { screen.getByTestId('notification-banner-dismiss').focus() })
    // Focus leaves the card; the pointer is still resting on it.
    act(() => { outside.focus() })
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS * 2) })
    expect(cards()).toHaveLength(1)
    // And it still hides once that last hold is released.
    fireEvent.pointerLeave(stack)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS + 1) })
    expect(cards()).toHaveLength(0)
  })

  it('starts the clock for the next card after a FOCUSED one was dismissed', () => {
    renderBanner()
    vi.useFakeTimers()
    arrive(mkN())
    // Dismissing the focused button removes it; no blur follows, so the hold
    // it took has to be released by the emptying deck or it outlives the card.
    act(() => { screen.getByTestId('notification-banner-dismiss').focus() })
    fireEvent.click(screen.getByTestId('notification-banner-dismiss'))
    expect(cards()).toHaveLength(0)
    arrive(mkN())
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS + 1) })
    expect(cards()).toHaveLength(0)
  })

  it('resumes the SURVIVING card after a focused one is dismissed out of a deck', () => {
    renderBanner()
    vi.useFakeTimers()
    arrive(mkN({ title: 'Older' }))
    arrive(mkN({ title: 'Newest' }))
    expect(cards()).toHaveLength(2)
    // Only the top card renders a real card (the rest are blank deck shells),
    // so this is the focus INSIDE the card that is about to be dismissed.
    act(() => { screen.getByTestId('notification-banner-dismiss').focus() })
    fireEvent.click(screen.getByTestId('notification-banner-dismiss'))
    // The deck is NOT empty, so an empty-deck reset never runs — and the
    // unmounted button fired no blur. The hold has no owner left.
    expect(cards()).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS * 2) })
    expect(cards()).toHaveLength(0)
  })

  it('keeps a focus hold a SURVIVING card still owns', () => {
    renderBanner()
    vi.useFakeTimers()
    arrive(mkN({ title: 'Older' }))
    arrive(mkN({ title: 'Newest' }))
    // Expanded, both render real cards, so focus can sit in the one that lives.
    fireEvent.click(screen.getByTestId('notification-banner-count'))
    const dismissals = screen.getAllByTestId('notification-banner-dismiss')
    expect(dismissals).toHaveLength(2)
    act(() => { dismissals[1].focus() })
    // Escape dismisses the TOP card; focus is in the second one, which stays.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(cards()).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS * 2) })
    expect(cards()).toHaveLength(1)
  })

  it('keeps the pointer hold when a card is removed under the cursor', () => {
    renderBanner()
    vi.useFakeTimers()
    arrive(mkN({ title: 'Older' }))
    arrive(mkN({ title: 'Newest' }))
    const stack = cards()[0].parentElement!
    fireEvent.pointerEnter(stack)
    fireEvent.keyDown(document, { key: 'Escape' })
    // The container's box did not move, so the pointer still rests on it and
    // its hold is still owned. Only a real pointerleave releases it.
    expect(cards()).toHaveLength(1)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS * 2) })
    expect(cards()).toHaveLength(1)
    fireEvent.pointerLeave(stack)
    act(() => { vi.advanceTimersByTime(BANNER_AUTO_HIDE_MS + 1) })
    expect(cards()).toHaveLength(0)
  })
})

describe('NotificationBanner: stack and interaction', () => {
  it('shows a deck of BLANK shells behind the top card, and expands on the deck edge', () => {
    renderBanner()
    arrive(mkN({ title: 'First', body: 'first body' }))
    arrive(mkN({ title: 'Second', body: 'second body' }))
    arrive(mkN({ title: 'Third', body: 'third body' }))
    const all = cards()
    expect(all).toHaveLength(3)
    // Newest on top, in full; the two older ones peek as the deck.
    expect(all[0].getAttribute('data-deck')).toBeNull()
    expect(all[0].textContent).toContain('Third')
    expect(all[1].getAttribute('data-deck')).toBe('true')
    expect(all[2].getAttribute('data-deck')).toBe('true')
    // The deck cards are material only: no title, body, icon or time can
    // print through the translucent top card.
    for (const shell of all.slice(1)) {
      expect(shell.textContent).toBe('')
      expect(shell.querySelector('svg')).toBeNull()
      expect(shell.querySelector('[data-notification-card]')).toBeNull()
    }
    expect(screen.queryByText('First')).toBeNull()
    expect(screen.queryByText('Second')).toBeNull()
    // The pill reads "N more" and is the finger-sized expand control; each
    // shell is the same control under the same name.
    const pill = screen.getByTestId('notification-banner-count')
    expect(pill.textContent).toBe('Show 2 more')
    expect(screen.getAllByRole('button', { name: 'Show 2 more notifications' })).toHaveLength(3)
    fireEvent.click(pill)
    expect(cards().every(c => c.getAttribute('data-deck') === null)).toBe(true)
    expect(screen.getByText('First')).toBeTruthy()
    expect(screen.queryByTestId('notification-banner-count')).toBeNull()
  })

  it('the pill uses the singular form for one hidden card', () => {
    renderBanner()
    arrive(mkN())
    arrive(mkN())
    expect(screen.getByTestId('notification-banner-count').textContent).toBe('Show 1 more')
    expect(screen.getAllByRole('button', { name: 'Show 1 more notification' })).toHaveLength(2)
  })

  it('a single pending card shows no count pill', () => {
    renderBanner()
    arrive(mkN())
    expect(screen.queryByTestId('notification-banner-count')).toBeNull()
  })

  it('folds the expanded list past four cards into a "+N more in your inbox" line that goes to the inbox page', () => {
    renderBanner()
    for (let i = 0; i < 6; i++) arrive(mkN())
    fireEvent.click(screen.getByTestId('notification-banner-count'))
    expect(cards()).toHaveLength(4)
    fireEvent.click(screen.getByText('+2 more in your inbox'))
    // The same place the popover's "Open inbox" goes, so the word names one place.
    expect(mockNavigate).toHaveBeenCalledWith('/notifications')
    expect(cards()).toHaveLength(0)
  })

  it('the inbox line is refused by the leave guard like any other navigation', () => {
    mayLeaveMock = false
    renderBanner()
    for (let i = 0; i < 6; i++) arrive(mkN())
    fireEvent.click(screen.getByTestId('notification-banner-count'))
    fireEvent.click(screen.getByText('+2 more in your inbox'))
    expect(mockNavigate).not.toHaveBeenCalled()
    expect(cards()).toHaveLength(4)
  })

  it('shows the newest card alone on mobile, with the close visible at rest', () => {
    isMobileMock = true
    renderBanner()
    arrive(mkN({ title: 'Older' }))
    arrive(mkN({ title: 'Newest' }))
    expect(cards()).toHaveLength(1)
    expect(cards()[0].textContent).toContain('Newest')
    // A touch screen has no hover, so the X cannot be hover-only there.
    const x = screen.getByTestId('notification-banner-dismiss')
    expect(x.className).not.toContain('opacity-0')
    expect(x.className).not.toContain('group-hover')
  })

  it('keeps the close hover-revealed on desktop', () => {
    renderBanner()
    arrive(mkN())
    const x = screen.getByTestId('notification-banner-dismiss')
    expect(x.className).toContain('opacity-0')
    expect(x.className).toContain('group-hover:opacity-50')
  })

  it('Escape dismisses the topmost card only', () => {
    renderBanner()
    arrive(mkN({ title: 'First' }))
    arrive(mkN({ title: 'Second' }))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(cards()).toHaveLength(1)
    expect(cards()[0].textContent).toContain('First')
  })

  it('a body click opens the bell on that note and removes the card', () => {
    const n = mkN({ title: 'Open me' })
    const { onOpenNote } = renderBanner()
    arrive(n)
    fireEvent.click(screen.getByRole('button', { name: 'Open notification: Open me' }))
    expect(onOpenNote).toHaveBeenCalledWith(n.ts)
    expect(cards()).toHaveLength(0)
  })

  it('renders at most two safe url actions and acks on action click', async () => {
    const n = mkN({
      actions: [
        { id: 'a', label: 'Open run', url: '/schedule' },
        { id: 'b', label: 'Bad', url: 'https://evil.example' },
        { id: 'c', label: 'Details', url: '/system' },
        { id: 'd', label: 'Fourth', url: '/apps' },
      ],
    })
    const { store } = renderBanner({ items: [n] })
    arrive(n)
    expect(screen.getByText('Open run')).toBeTruthy()
    expect(screen.getByText('Details')).toBeTruthy()
    expect(screen.queryByText('Bad')).toBeNull()
    expect(screen.queryByText('Fourth')).toBeNull()
    fireEvent.click(screen.getByText('Details'))
    await waitFor(() => expect(cards()).toHaveLength(0))
    expect(store.getState().notifications.items[0].acked).toBe(true)
    expect(mockAck).toHaveBeenCalledWith(n.ts)
  })

  it('an action refused by the leave guard acks nothing and keeps the card', async () => {
    mayLeaveMock = false
    const n = mkN({ actions: [{ id: 'a', label: 'Open run', url: '/schedule' }] })
    const { store } = renderBanner({ items: [n] })
    arrive(n)
    fireEvent.click(screen.getByText('Open run'))
    await act(async () => { await Promise.resolve() })
    expect(mockAck).not.toHaveBeenCalled()
    expect(store.getState().notifications.items[0].acked).toBe(false)
    expect(cards()).toHaveLength(1)
  })

  it('a rejected ack restores unread, keeps the card and shows a notice; a retry clears it', async () => {
    mockAck.mockRejectedValueOnce(new Error('network'))
    const n = mkN({ actions: [{ id: 'a', label: 'Open run', url: '/schedule' }] })
    const { store } = renderBanner({ items: [n] })
    arrive(n)
    fireEvent.click(screen.getByText('Open run'))
    const notice = await screen.findByTestId('notification-banner-ack-failed')
    expect(notice.textContent).toContain("Couldn't mark this as read. Try the action again.")
    // The optimistic flip was undone: the server still holds it unread.
    expect(store.getState().notifications.items[0].acked).toBe(false)
    expect(cards()).toHaveLength(1)
    // The action itself is the retry.
    fireEvent.click(screen.getByText('Open run'))
    await waitFor(() => expect(cards()).toHaveLength(0))
    expect(store.getState().notifications.items[0].acked).toBe(true)
  })

  it('an approval card offers a single Review action that opens the bell on it', () => {
    const n = mkN({ kind: 'approval', priority: 'critical' })
    const { onOpenNote } = renderBanner()
    arrive(n)
    fireEvent.click(screen.getByRole('button', { name: 'Review' }))
    expect(onOpenNote).toHaveBeenCalledWith(n.ts)
  })
})

describe('exit geometry', () => {
  const card = { left: 900, right: 1240, top: 60, bottom: 130, width: 340, height: 70 } as DOMRect
  const bell = { left: 1250, right: 1278, top: 8, bottom: 36, width: 28, height: 28 } as DOMRect

  it('aims the top-right corner of the card at the bell centre', () => {
    expect(computeExitDelta(card, bell)).toEqual({ dx: 1264 - 1240, dy: 22 - 60 })
  })

  it('travels and shrinks with motion, fades only under reduced motion or without a measured bell', () => {
    const full = exitTarget({ dx: 24, dy: -38 }, false)
    expect(full).toMatchObject({ x: 24, y: -38, opacity: 0 })
    expect(full.scale).toBeLessThan(1)
    const reduced = exitTarget({ dx: 24, dy: -38 }, true)
    expect(reduced).toEqual({ opacity: 0, transition: { duration: 0.18 } })
    expect(Object.keys(reduced)).not.toContain('x')
    expect(Object.keys(reduced)).not.toContain('scale')
    expect(exitTarget(undefined, false)).toEqual({ opacity: 0, transition: { duration: 0.18 } })
  })
})
