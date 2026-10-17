/**
 * Notification Center sheet — a press on its own background dismisses it.
 *
 * The sheet is transparent by design: the panel paints nothing and every
 * readable element is a floating card. The outside-press handler used to judge
 * a press by which BOX contained it, and on a phone the popover's box is the
 * whole viewport under the top bar, so nothing but the bell could dismiss the
 * sheet; on desktop the empty strip below the last card sat inside the 400px
 * column and stayed inert while the identical-looking strip left of the column
 * dismissed. The handler now judges a press by what it LANDED ON: material,
 * rows and controls keep the sheet open, anything else inside the popover is
 * its background and dismisses it exactly like a press outside would. A
 * "press" is the whole gesture: pointerdown AND pointerup on the background,
 * completed by a click there, with no text selected on the way — a drag that
 * ran from a card onto the background, from the background onto a card, or
 * between two background points across a card's text is a selection, and the
 * click it ends with is not a dismissal.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import type { Notification } from '../types'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))

// What the boot fetch hands the sheet, swapped per test to match the seeded
// store so a late fallback fetch re-delivers the same rows.
const feed = vi.hoisted(() => ({ notes: [] as unknown[] }))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn(() => Promise.resolve({ notifications: feed.notes })),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import App from '../App'

// Already read, so selecting it does not fire the auto-ack request.
const NOTE: Notification = { kind: 'cron', ts: '2026-07-23T10:00:00Z', title: 'Nightly digest ready', body: 'Digest body', acked: true }

/**
 * A FRESH matchMedia per call: `useIsMobile` caches its MediaQueryList on the
 * function's identity, so re-pointing the same mock would not move the
 * breakpoint between the phone and desktop cases below.
 */
function viewport(mobile: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: mobile ? /max-width:\s*767px/.test(query) : query === '(prefers-color-scheme: dark)',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  })
}

const sheet = () => document.querySelector('[data-nc-phase]') as HTMLElement | null
const phase = () => sheet()?.getAttribute('data-nc-phase') ?? null

async function openSheet({ mobile, notes }: { mobile: boolean; notes: Notification[] }) {
  feed.notes = notes
  viewport(mobile)
  // Seeded rather than fetched: the boot fetch is a 5 s fallback behind the
  // WebSocket, which these tests mock away.
  const store = createTestStore({ notifications: { items: notes, clearSeq: 0, ackSeq: 0, ackSeqByTs: {} } })
  renderWithProviders(<App />, { route: '/chat', store })
  const bell = await screen.findByLabelText('Notifications')
  fireEvent.click(bell)
  expect(phase()).toBe('open')
  if (notes.length > 0) await screen.findByText(notes[0].title)
  return bell
}

/** A press at a viewport point, the way a real pointer reports one: the
 *  pointerdown, the pointerup and the click that complete the same gesture. */
function pressAt(el: Element, clientX: number, clientY: number) {
  fireEvent(el, new PointerEvent('pointerdown', { bubbles: true, cancelable: true, clientX, clientY }))
  fireEvent(el, new PointerEvent('pointerup', { bubbles: true, cancelable: true, clientX, clientY }))
  fireEvent(el, new MouseEvent('click', { bubbles: true, cancelable: true, clientX, clientY }))
}

/** A tap that begins and ends on `el`. */
function press(el: Element) {
  fireEvent.pointerDown(el)
  fireEvent.pointerUp(el)
  fireEvent.click(el)
}

/** What a drag leaves behind when it swept over text: a non-collapsed
 *  document selection covering `el`'s contents. */
function selectTextOf(el: Element) {
  const range = document.createRange()
  range.selectNodeContents(el)
  const selection = window.getSelection()!
  selection.removeAllRanges()
  selection.addRange(range)
  expect(selection.isCollapsed, 'the test needs a real selection to stand for the drag').toBe(false)
}

describe('Notification Center sheet — a press on its background dismisses it', () => {
  afterEach(() => { feed.notes = []; window.getSelection()?.removeAllRanges() })

  it('phone: a tap on the empty area under the cards dismisses the full-width sheet', async () => {
    await openSheet({ mobile: true, notes: [] })
    // Full width: on a phone the sheet IS the popover's box, which is why a
    // box test could never see an outside press here.
    expect(sheet()!.classList.contains('w-full')).toBe(true)

    // The empty inbox's label floats directly on the transparent background
    // (it is not a card), so it is the real hit target of a tap below the
    // controls card on an empty sheet.
    press(screen.getByTestId('notification-feed-empty'))
    expect(phase(), 'a background tap must dismiss like an outside press').toBe('closing')
  })

  it('desktop: a press in the empty strip below the last card dismisses the column', async () => {
    await openSheet({ mobile: false, notes: [NOTE] })
    expect(sheet()!.classList.contains('w-[400px]')).toBe(true)

    // The list container fills the column under the last card, so it is what
    // a press in that strip lands on.
    press(screen.getByTestId('notification-feed-list'))
    expect(phase(), 'the strip inside the column must behave like the strip beside it').toBe('closing')
  })

  it('dismisses at the click, not at the pointerdown, so the gesture ends on the sheet', async () => {
    await openSheet({ mobile: true, notes: [NOTE] })
    const list = screen.getByTestId('notification-feed-list')

    // Still open between the halves: a sheet that left at pointerdown was
    // pointer-transparent by the time the tap's click arrived, and that click
    // landed on whatever sat under the transparent strip.
    fireEvent.pointerDown(list)
    expect(phase(), 'the sheet must still take the click that completes the tap').toBe('open')
    fireEvent.pointerUp(list)
    expect(phase()).toBe('open')
    fireEvent.click(list)
    expect(phase()).toBe('closing')
  })

  it('keeps the sheet open for a press on a row, on a control, or on the detail panel', async () => {
    await openSheet({ mobile: false, notes: [NOTE] })

    // A row (its title text, inside the floating card); its click would select
    // the row, which the detail-panel step below does deliberately.
    fireEvent.pointerDown(screen.getByText(NOTE.title))
    expect(phase(), 'a press on a card must not dismiss').toBe('open')

    // A control inside the controls card.
    press(screen.getByPlaceholderText('Search…'))
    expect(phase(), 'a press on a control must not dismiss').toBe('open')
    fireEvent.pointerDown(screen.getByText('Open inbox'))
    expect(phase()).toBe('open')

    // The detail panel a selected row slides out.
    fireEvent.click(screen.getByText(NOTE.title))
    const close = await screen.findByText('Close')
    press(close.closest('button')!.parentElement!)
    expect(phase(), 'a press on the detail panel must not dismiss').toBe('open')
  })

  it('keeps the sheet open for a drag that starts on a card and ends on the background', async () => {
    await openSheet({ mobile: false, notes: [NOTE] })

    // Selecting a row's text and releasing past the card's edge clicks the
    // common ancestor of the two points — the list, which is background. The
    // gesture began on a card, so it is not a background press.
    fireEvent.pointerDown(screen.getByText(NOTE.title))
    fireEvent.pointerUp(screen.getByTestId('notification-feed-list'))
    fireEvent.click(screen.getByTestId('notification-feed-list'))
    expect(phase(), 'a drag out of a card must not dismiss').toBe('open')
  })

  it('keeps the sheet open for a drag that starts on the background and ends on a card', async () => {
    await openSheet({ mobile: false, notes: [NOTE] })
    const list = screen.getByTestId('notification-feed-list')
    const title = screen.getByText(NOTE.title)

    // The mirror image of the case above: the drag BEGINS in the strip below
    // the cards, sweeps up into a row and is released on it. The click again
    // lands on the common ancestor — the background list — but the gesture
    // ended on material, so it is not a background press. Pinned WITHOUT a
    // selection (a release on a card's padding selects nothing), so that it
    // is the two ends of the gesture that decide, not what the sweep left.
    fireEvent.pointerDown(list)
    fireEvent.pointerUp(title)
    fireEvent.click(list)
    expect(phase(), 'a drag that began on the background and ended on a card must not dismiss').toBe('open')

    // The usual outcome of that drag — the row's text is now selected — must
    // not dismiss either, whichever rule catches it.
    fireEvent.pointerDown(list)
    selectTextOf(title)
    fireEvent.pointerUp(title)
    fireEvent.click(list)
    expect(phase(), 'a drag that selected a card\'s text must not dismiss mid-selection').toBe('open')
  })

  it('keeps the sheet open when a drag between two background points swept text into a selection', async () => {
    await openSheet({ mobile: false, notes: [NOTE] })
    const list = screen.getByTestId('notification-feed-list')

    // From the strip below the cards up to the gap between groups: both ends
    // are background, and everything in between was selected on the way. The
    // selection is the user's intent, not a dismissal.
    fireEvent.pointerDown(list)
    selectTextOf(screen.getByText(NOTE.title))
    fireEvent.pointerUp(list)
    fireEvent.click(list)
    expect(phase(), 'a drag that selected text must not dismiss, wherever it began and ended').toBe('open')

    // The same two points with nothing selected are the plain tap they look like.
    window.getSelection()!.removeAllRanges()
    press(list)
    expect(phase(), 'without a selection the same gesture is a background tap').toBe('closing')
  })

  it('keeps the sheet open for a press on the list\'s own scrollbar', async () => {
    await openSheet({ mobile: false, notes: [NOTE] })
    const list = screen.getByTestId('notification-feed-list')

    // A classic scrollbar hit-tests to the element it scrolls, so a press on
    // the thumb has the SAME target as a press on the empty strip. Give the
    // list the geometry of an overflowing 390px-wide box with a 6px bar (the
    // dashboard's ::-webkit-scrollbar width) and tell the two apart by where
    // the pointer is.
    Object.defineProperty(list, 'clientWidth', { value: 384, configurable: true })
    Object.defineProperty(list, 'clientHeight', { value: 600, configurable: true })
    Object.defineProperty(list, 'scrollHeight', { value: 1400, configurable: true })
    list.getBoundingClientRect = () => ({ x: 0, y: 48, left: 0, top: 48, right: 390, bottom: 648, width: 390, height: 600, toJSON() { return {} } }) as DOMRect

    pressAt(list, 387, 300)
    expect(phase(), 'a grab of the scrollbar must not dismiss').toBe('open')

    pressAt(list, 200, 300)
    expect(phase(), 'the same target inside the client box is the background').toBe('closing')
  })

  it('leaves the bell toggle alone: a press on the bell keeps it, the click closes it', async () => {
    const bell = await openSheet({ mobile: true, notes: [] })

    fireEvent.pointerDown(bell)
    expect(phase()).toBe('open')
    fireEvent.click(bell)
    expect(phase()).toBe('closing')
  })
})

/**
 * The allowlist in `isSheetBackgroundPress` makes DEFAULT-DISMISS the fate of any
 * child composed into the sheet without a marker: a future strip, banner or
 * label that is neither material nor a decision would dismiss the sheet when
 * pressed, and nothing else would notice. This walk is what notices. Every
 * element a press can land on must be material (the selector below), or
 * background by a decision recorded at its composition site: the list is the
 * strip a press below the last card lands on, the empty inbox's label floats
 * directly on the background, and a group heading is not a card. Anything else
 * that renders its own text is unmarked, and fails here by name.
 */
describe('Notification Center sheet — everything composed into it is material or background by decision', () => {
  // The verdict `isSheetBackgroundPress` gives: a press on a match keeps the
  // sheet, a press on anything else inside the popover dismisses it.
  const MATERIAL = '.notif-material, [data-notif-row], [data-nc-material], button, a, input, textarea, select, [role="button"]'
  const BACKGROUND_CONTAINER = '[data-testid="notification-feed-list"]'
  const BACKGROUND_LABEL = '[data-testid="notification-feed-empty"]'

  const ownText = (el: Element) => Array.from(el.childNodes)
    .filter(n => n.nodeType === Node.TEXT_NODE)
    .map(n => n.textContent ?? '')
    .join('')
    .trim()
  // A group heading is the first child of a group, and a group is a child of the list.
  const isGroupHeading = (el: Element) => {
    const group = el.parentElement
    return !!group && !!group.parentElement?.matches(BACKGROUND_CONTAINER) && group.firstElementChild === el
  }

  function walk(root: Element) {
    const seen = { material: 0, headings: 0, unmarked: [] as string[] }
    const visit = (el: Element) => {
      // Cannot be pressed at all (the column scrim).
      if (el.classList.contains('pointer-events-none')) return
      // Keeps the sheet; what is inside a card is the card's business.
      if (el.matches(MATERIAL)) { seen.material += 1; return }
      if (el.matches(BACKGROUND_LABEL)) return
      if (isGroupHeading(el)) { seen.headings += 1; return }
      const text = ownText(el)
      if (text && !el.matches(BACKGROUND_CONTAINER)) seen.unmarked.push(`<${el.tagName.toLowerCase()}> "${text.slice(0, 40)}"`)
      for (const child of Array.from(el.children)) visit(child)
    }
    for (const child of Array.from(root.children)) visit(child)
    return seen
  }

  afterEach(() => { feed.notes = [] })

  it('with rows: cards and controls are material, the list and its group heading are background by decision', async () => {
    await openSheet({ mobile: false, notes: [NOTE] })
    const seen = walk(sheet()!)
    expect(seen.unmarked, 'an element composed into the sheet is neither material nor background by decision').toEqual([])
    // The walk saw the real structure, not an empty tree.
    expect(seen.material, 'the controls card and the row').toBeGreaterThanOrEqual(2)
    expect(seen.headings).toBe(1)

    // The popover's other child, the detail panel a selected row slides out, is material too.
    fireEvent.click(screen.getByText(NOTE.title))
    await screen.findByText('Close')
    const others = Array.from(sheet()!.parentElement!.children).filter(el => el !== sheet())
    expect(others).toHaveLength(1)
    expect(others[0].matches(MATERIAL)).toBe(true)
  })

  it('empty inbox: the label floats on the background by decision, and nothing else is unmarked', async () => {
    await openSheet({ mobile: true, notes: [] })
    const seen = walk(sheet()!)
    expect(seen.unmarked).toEqual([])
    expect(seen.material, 'the controls card').toBeGreaterThanOrEqual(1)
    expect(sheet()!.querySelector(BACKGROUND_LABEL)).not.toBeNull()
  })
})
