/**
 * The row under the pointer holds its position while the rest of the list keeps
 * sorting around it.
 *
 * Under the default date-desc sort, a background session going active re-sorts
 * the sidebar at any moment. A row that moves out from under the cursor between
 * the user reading it and pressing turns the hover action bar into a mis-click
 * generator — the close button in particular, where the mis-click closes a
 * session. So the hovered row keeps the PIXEL OFFSET it had when the pointer
 * arrived, and takes its true place once the pointer leaves.
 *
 * Locks the contract:
 *  (1) The hovered row holds its position while newer rows sort in around it.
 *  (2) It takes its true index on release — the hold defers one row's position,
 *      it does not suppress the sort.
 *  (3) Mouse only: a touch pointerover must not hold anything.
 *  (4) A held row that unmounts releases the hold rather than leaking it.
 *  (5) The held slot is the one the POINTER ARRIVED OVER, even when the re-sort
 *      lands in the same batch as the pointerover — reading the slot off that
 *      render would capture the post-sort position and hold nothing.
 *  (6) The anchor is the row's PIXEL OFFSET, not its ordinal: rows are unequal
 *      height, so holding the index lets a taller row sorting in above push the
 *      held row out from under a stationary cursor.
 *  (7) The pin clears when the row leaves the RENDERED set — a filter hiding it
 *      counts, not only an unmount — so a stale order is never reapplied.
 *  (8) A held row emits no date-segment header WHERE THAT HEADER WAS ALREADY
 *      PASSED, so the walk cannot restate one — but a bucket the held row opens
 *      (its sole row, or the lane's top row) keeps the only header it has.
 *  (9) DATE HEADERS COUNT TOWARD THAT OFFSET. A bucket collapsing above the held
 *      row shifts it by a header's height, which row heights alone cannot see.
 * (10) The pin is scoped: it clears when the row stops being rendered IN THE
 *      PINNED SCOPE, so a lane switch cannot bank a stale order for its return.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatFolder, ChatSlot } from '../types'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown>>((props, ref) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
// Legacy single-lane list (no tag columns) keeps the rows in one queryable list.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({ folders: [] as unknown[] }))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, p: string) => {
      if (p === 'chatFolders') return vi.fn().mockImplementation(() => Promise.resolve(mocks.folders))
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import type { RootState } from '../store'

// Local midday, not 12:00Z: the farthest point from both midnight edges in any
// timezone, so a `-N days` fixture lands in its intended calendar bucket
// regardless of where the suite runs. Mid-January avoids DST in the lookback.
const PIN = new Date(2026, 0, 15, 12, 0, 0)
const MIN = 60_000
const HOUR = 3_600_000
const DAY = 86_400_000
const ago = (ms: number) => new Date(PIN.getTime() - ms).toISOString()

const slot = (key: string, title: string, lastTs: string): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '', last_ts: lastTs, pinned: false,
} as unknown as ChatSlot)

// date-desc: A (1h ago), B (3d), C (20d). Deliberately straddles three date
// buckets — Today / Last 7 Days / Last 30 Days — so the segment-header
// assertion below has something to restate if the exclusion is missing.
const A = slot('chat-a', 'Alpha session', ago(HOUR))
const B = slot('chat-b', 'Bravo session', ago(3 * DAY))
const C = slot('chat-c', 'Charlie session', ago(20 * DAY))
const SLOTS_INITIAL = [A, B, C]

// C goes active and becomes the newest of all three → true order is C, A, B.
const C_BUMPED = slot('chat-c', 'Charlie session', ago(MIN))
const SLOTS_REORDERED = [A, B, C_BUMPED]

function renderSidebar(slots: ChatSlot[], folders: ChatFolder[] = []) {
  mocks.folders = folders
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  const tree = (s: ChatSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={s} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const { rerender } = render(tree(slots))
  return { rerender: (s: ChatSlot[]) => rerender(tree(s)) }
}

/** Session-row keys in document order. */
const renderedKeys = () =>
  Array.from(document.querySelectorAll('[data-session-row]')).map(el => el.getAttribute('data-session-row'))

const rowFor = (key: string) => {
  const el = document.querySelector(`[data-session-row="${key}"]`)
  if (!el) throw new Error(`no rendered row for ${key}`)
  return el
}

const hover = (key: string, pointerType = 'mouse') =>
  fireEvent.pointerOver(rowFor(key), { pointerType, bubbles: true })

const leaveSidebar = () => {
  const root = document.querySelector('.sidebar-inner')
  if (!root) throw new Error('no sidebar root')
  fireEvent.pointerLeave(root, { pointerType: 'mouse' })
}

beforeEach(() => {
  localStorage.clear()
  // Fixtures carry old timestamps; keep the stale-session collapse off so every
  // row stays in the one queryable list.
  localStorage.setItem('mc-session-stale-collapse-ms', '0')
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(PIN)
})
afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('ChatSidebar – the hovered row holds its position', () => {
  it('holds the hovered row while the rest re-sorts, and releases on leave', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    hover('chat-b')

    // C becomes the newest, so the sort wants C, A, B. B is held at index 1,
    // and A takes the slot B would have moved into. This is the assertion that
    // fails without the hold — B would slide to the bottom under the cursor.
    rerender(SLOTS_REORDERED)
    expect(renderedKeys()).toEqual(['chat-c', 'chat-b', 'chat-a'])

    // The hold defers B's position; it does not suppress the sort.
    leaveSidebar()
    expect(renderedKeys()).toEqual(['chat-c', 'chat-a', 'chat-b'])
  })

  it('moves the hold when the pointer travels to another row', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    hover('chat-b')
    hover('chat-c')
    // Now C is held at index 2, so the bump cannot lift it to the top.
    rerender(SLOTS_REORDERED)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])
  })

  it('does NOT hold for a touch pointer (negative control)', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    hover('chat-b', 'touch')
    rerender(SLOTS_REORDERED)
    // Unheld, so the sort lands in full.
    expect(renderedKeys()).toEqual(['chat-c', 'chat-a', 'chat-b'])
  })

  it('DOES hold for a pen pointer, which reveals the same action bar', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    // A pen hovers, so group-hover exposes Close exactly as it does for a mouse;
    // excluding pen would leave pen users the mis-click this guard prevents.
    hover('chat-b', 'pen')
    rerender(SLOTS_REORDERED)
    expect(renderedKeys()).toEqual(['chat-c', 'chat-b', 'chat-a'])
  })

  it('releases the hold when the held row unmounts', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    hover('chat-b')
    // B closes while held. Nothing emits pointerleave for a removed node, so a
    // leaked hold would pin the order on a row that no longer exists.
    rerender([A, C_BUMPED])
    expect(renderedKeys()).toEqual(['chat-c', 'chat-a'])
  })

  it('holds the slot the pointer arrived over, not one the list re-sorted into', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    // The bump lands in the SAME batch as the pointerover, so the first render
    // carrying the pin already re-sorted — a capture there would hold nothing.
    act(() => {
      // Raw dispatch: fireEvent flushes on its own, splitting this into 2 renders.
      const ev = new MouseEvent('pointerover', { bubbles: true })
      Object.defineProperty(ev, 'pointerType', { value: 'mouse' })
      rowFor('chat-b').dispatchEvent(ev)
      rerender(SLOTS_REORDERED)
    })
    expect(renderedKeys()).toEqual(['chat-c', 'chat-b', 'chat-a'])
  })

  it('the fixture really does reorder under date-desc (guards the assertions above)', () => {
    // Without this, a bump that failed to change the sort would make every
    // "held" assertion pass vacuously.
    const order = (slots: ChatSlot[]) => [...slots]
      .sort((a, b) => new Date(b.last_ts!).getTime() - new Date(a.last_ts!).getTime())
      .map(s => s.key)
    expect(order(SLOTS_INITIAL)).toEqual(['chat-a', 'chat-b', 'chat-c'])
    expect(order(SLOTS_REORDERED)).toEqual(['chat-c', 'chat-a', 'chat-b'])
  })
})

describe('ChatSidebar – two ordering overrides armed at once', () => {
  // The digit-jump freeze holds the SHORTCUT order; the hover pin holds the
  // RENDERED order. Nothing previously tested them together.
  const badgeByKey = () => {
    const out: Record<string, string> = {}
    for (const el of Array.from(document.querySelectorAll('[data-session-row]'))) {
      const badge = el.querySelector('[data-testid="digit-jump-badge"]')
      const key = el.getAttribute('data-session-row')
      if (badge?.textContent && key) out[key] = badge.textContent
    }
    return out
  }

  it('the digit freeze keeps its own order while the hover pin holds the rendered one', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    hover('chat-b')
    // Arm the digit modifier too: both ctrl and alt set so either platform mapping
    // matches, and location 1 marks it as the modifier key itself.
    fireEvent.keyDown(window, { altKey: true, ctrlKey: true, location: 1 })
    expect(badgeByKey()).toEqual({ 'chat-a': '1', 'chat-b': '2', 'chat-c': '3' })

    rerender(SLOTS_REORDERED)

    // Precedence: the pin governs the rendered order (B keeps its slot)…
    expect(renderedKeys()).toEqual(['chat-c', 'chat-b', 'chat-a'])
    // …while the digit freeze still governs the jump mapping, so the digits stay
    // on the rows the user read them from rather than following the new order.
    expect(badgeByKey()).toEqual({ 'chat-a': '1', 'chat-b': '2', 'chat-c': '3' })
  })
})

describe('ChatSidebar – the hold anchors on pixel position, not ordinal', () => {
  let restoreRects: (() => void) | null = null
  // Rows are unequal height in practice (an expanded source-chip row is taller),
  // and jsdom reports every height as 0, so the geometry path needs real numbers.
  const stubRowHeights = (byKey: Record<string, number>) => {
    const orig = Element.prototype.getBoundingClientRect
    Element.prototype.getBoundingClientRect = function (this: Element) {
      const key = this.getAttribute?.('data-session-row')
      const h = key ? byKey[key] : undefined
      if (h == null) return orig.call(this)
      return { height: h, width: 240, top: 0, left: 0, right: 240, bottom: h, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
    }
    restoreRects = () => { Element.prototype.getBoundingClientRect = orig }
  }
  afterEach(() => { restoreRects?.(); restoreRects = null })

  it('keeps the held row near its pixel offset when a TALL row sorts in above it', () => {
    // Same fixture and same interaction as the ordinal test above; the ONLY
    // difference is that chat-c is tall, which is what separates the two anchors.
    stubRowHeights({ 'chat-a': 40, 'chat-b': 40, 'chat-c': 200 })
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    hover('chat-b')
    rerender(SLOTS_REORDERED)

    // B sat at 40px. Holding its ORDINAL (1) puts the 200px C above it instead, so
    // B slides 160px down; slot 0 is 40px off, the closest the anchor can get.
    expect(renderedKeys()).toEqual(['chat-b', 'chat-c', 'chat-a'])
  })

  it('keeps the captured offset when a row ABOVE the held one closes', () => {
    stubRowHeights({ 'chat-a': 40, 'chat-b': 40, 'chat-c': 40 })
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    hover('chat-b')
    // A closes in another tab, so it leaves the list while the hold is armed.
    rerender([B, C])

    // B was read 40px down. Summing the LIVE list drops A's 40px with it and slides
    // B to offset 0, under a cursor that never moved; below C is the only 40px left.
    expect(renderedKeys()).toEqual(['chat-c', 'chat-b'])
  })

  it('clears the pin when a filter hides the hovered row, not just when it unmounts', () => {
    localStorage.setItem('mc-session-running-only', '1')
    const run = (s: ChatSlot, running: boolean) => ({ ...s, running }) as ChatSlot
    const { rerender } = renderSidebar([run(A, true), run(B, true), run(C, true)])
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    hover('chat-b')
    // B stops running, so it leaves the RENDERED set while remaining in `slots` —
    // a release keyed on `slots` never fires here and the pin survives.
    rerender([run(A, true), run(B, false), run(C, true)])
    expect(renderedKeys()).toEqual(['chat-a', 'chat-c'])

    // B returns while C is now newest. Pin cleared ⇒ the order is fully live; a
    // surviving pin would reapply B's stale slot and yield chat-c, chat-b, chat-a.
    rerender([run(A, true), run(B, true), run(C_BUMPED, true)])
    expect(renderedKeys()).toEqual(['chat-c', 'chat-a', 'chat-b'])
  })
})

describe('ChatSidebar – a held DORMANT row does not cross its expander', () => {
  it('keeps the held row behind the expander when a prompt would refile it', () => {
    // Threshold 7d, so C (20d) is dormant while A and B are not.
    localStorage.setItem('mc-session-stale-collapse-ms', String(7 * DAY))
    const { rerender } = renderSidebar(SLOTS_INITIAL)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b'])

    const expander = document.querySelector('[data-testid="stale-expander-root"]')
    if (!expander) throw new Error('no dormant expander')
    fireEvent.click(expander)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    hover('chat-c')
    // The prompt lands: C is now the newest session, so the split refiles it fresh.
    rerender(SLOTS_REORDERED)

    // Held, it must stay where the pointer found it. Crossing into the fresh lane
    // moves it above the expander — geometry that lane's extent cannot express.
    expect(rowFor('chat-c').closest('[data-stale-region]')).not.toBeNull()
    expect(document.querySelector('[data-testid="stale-expander-root"]')).not.toBeNull()
  })
})

describe('ChatSidebar – a held row anchors within its own folder', () => {
  // Two folders in the tree. Every row in both carries data-session-scope="list"
  // because the arrow rove spans them, so only the container marker separates them.
  const TWO_FOLDERS: ChatFolder[] = [
    { id: 'f1', name: 'Alpha', order: 0 } as ChatFolder,
    { id: 'f2', name: 'Bravo', order: 1 } as ChatFolder,
  ]
  const inFolder = (key: string, fid: string, lastTs: string): ChatSlot =>
    ({ ...slot(key, key, lastTs), folder_id: fid } as unknown as ChatSlot)

  const F1A = inFolder('f1-a', 'f1', ago(2 * HOUR))
  const F1B = inFolder('f1-b', 'f1', ago(3 * HOUR))
  const F2A = inFolder('f2-a', 'f2', ago(4 * HOUR))
  const F2B = inFolder('f2-b', 'f2', ago(5 * HOUR))
  // f2-b becomes the newest in its folder, so the sort wants it above f2-a.
  const F2B_BUMPED = inFolder('f2-b', 'f2', ago(MIN))

  let restoreRects: (() => void) | null = null
  const stubRowHeights = (byKey: Record<string, number>) => {
    const orig = Element.prototype.getBoundingClientRect
    Element.prototype.getBoundingClientRect = function (this: Element) {
      const key = this.getAttribute?.('data-session-row')
      const h = key ? byKey[key] : undefined
      if (h == null) return orig.call(this)
      return { height: h, width: 240, top: 0, left: 0, right: 240, bottom: h, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
    }
    restoreRects = () => { Element.prototype.getBoundingClientRect = orig }
  }
  afterEach(() => { restoreRects?.(); restoreRects = null })

  const HEIGHTS = { 'f1-a': 40, 'f1-b': 40, 'f2-a': 40, 'f2-b': 40 }

  it('holds the first row of the SECOND folder at the top of that folder', () => {
    // Heights are what force the pixel path: the ordinal fallback compares ranks,
    // which stay ordered across containers, so it cannot see this defect at all.
    stubRowHeights(HEIGHTS)
    const { rerender } = renderSidebar([F1A, F1B, F2A, F2B], TWO_FOLDERS)
    expect(renderedKeys()).toEqual(['f1-a', 'f1-b', 'f2-a', 'f2-b'])

    hover('f2-a')
    rerender([F1A, F1B, F2A, F2B_BUMPED])

    // f2-a was read at offset 0 of ITS folder. Counting the two Alpha rows into the
    // anchor puts it 80px down, and the only seat that far into Bravo is the bottom.
    expect(renderedKeys()).toEqual(['f1-a', 'f1-b', 'f2-a', 'f2-b'])
  })

  it('the second folder really does reorder unheld (guards the assertion above)', () => {
    stubRowHeights(HEIGHTS)
    const { rerender } = renderSidebar([F1A, F1B, F2A, F2B], TWO_FOLDERS)
    rerender([F1A, F1B, F2A, F2B_BUMPED])
    expect(renderedKeys()).toEqual(['f1-a', 'f1-b', 'f2-b', 'f2-a'])
  })
})

describe('ChatSidebar – a held row emits no date-segment header', () => {
  // One folder so the flat-view toggle exists; the flat lane is the only slot
  // list that renders date segments.
  const FOLDERS: ChatFolder[] = [{ id: 'f1', name: 'Alpha', order: 0 } as ChatFolder]

  const openFlatLane = () => {
    const toggle = document.querySelector('[data-testid="flat-view-toggle"]')
    if (!toggle) throw new Error('no flat-view toggle')
    fireEvent.click(toggle)
    if (!document.querySelector('[data-testid="flat-view-lane"]')) throw new Error('flat lane did not open')
  }
  const headers = () =>
    Array.from(document.querySelectorAll('[data-testid="date-segment-header"]')).map(el => el.textContent)

  let restoreHeaderRects: (() => void) | null = null
  afterEach(() => { restoreHeaderRects?.(); restoreHeaderRects = null })

  it('counts a COLLAPSING date header in the held row pixel offset', () => {
    // Rows 40px, headers 100px, so one vanished header outweighs one row and the
    // two anchors cannot agree by coincidence.
    const orig = Element.prototype.getBoundingClientRect
    Element.prototype.getBoundingClientRect = function (this: Element) {
      const isRow = this.getAttribute?.('data-session-row')
      const isHdr = this.hasAttribute?.('data-date-header')
      if (!isRow && !isHdr) return orig.call(this)
      const h = isHdr ? 100 : 40
      return { height: h, width: 240, top: 0, left: 0, right: 240, bottom: h, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
    }
    restoreHeaderRects = () => { Element.prototype.getBoundingClientRect = orig }

    const D = slot('chat-d', 'Delta session', ago(21 * DAY))
    const B_BUMPED = slot('chat-b', 'Bravo session', ago(MIN))
    const { rerender } = renderSidebar([A, B, C, D], FOLDERS)
    openFlatLane()
    // Positive control: three buckets, so headers are genuinely in the geometry.
    expect(headers()).toHaveLength(3)
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c', 'chat-d'])

    hover('chat-c')
    // B jumps to Today, emptying its own bucket — so the header that sat directly
    // above C disappears and every row below it rises by 100px.
    rerender([A, B_BUMPED, C, D], FOLDERS)

    expect(headers()).toHaveLength(2)
    // C was at 380px (2 rows + 3 headers); its live slot is now 280px and below D
    // is 320px, the closest. Counting rows only makes 280px look exact.
    expect(renderedKeys()).toEqual(['chat-b', 'chat-a', 'chat-d', 'chat-c'])
  })

  it('clears the pin when the lane it was scoped to stops rendering the row', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL, FOLDERS)
    openFlatLane()
    expect(renderedKeys()).toEqual(['chat-a', 'chat-b', 'chat-c'])

    hover('chat-b')
    // Leaving flat view unmounts the 'flat' scope. chat-b is still in the slots the
    // release used to consult, so a key-only check keeps the pin alive here.
    const toggle = document.querySelector('[data-testid="flat-view-toggle"]')
    if (!toggle) throw new Error('no flat-view toggle')
    fireEvent.click(toggle)
    expect(document.querySelector('[data-testid="flat-view-lane"]')).toBeNull()

    openFlatLane()
    rerender(SLOTS_REORDERED, FOLDERS)
    // Pin cleared ⇒ fully live order. A surviving pin would hold B at its old slot
    // and render chat-c, chat-b, chat-a instead.
    expect(renderedKeys()).toEqual(['chat-c', 'chat-a', 'chat-b'])
  })

  it('never restates a header it already passed', () => {
    const { rerender } = renderSidebar(SLOTS_INITIAL, FOLDERS)
    openFlatLane()

    // Positive control: the fixture straddles three buckets, so the header path
    // is genuinely live. Without this a zero-header lane would pass silently.
    const before = headers()
    expect(before).toHaveLength(3)
    expect(new Set(before).size).toBe(3)

    hover('chat-b')
    rerender(SLOTS_REORDERED)

    // B is held between two Today rows. Left in the segment walk it would emit
    // its own header and force Today to be restated below it.
    expect(renderedKeys()).toEqual(['chat-c', 'chat-b', 'chat-a'])
    const after = headers()
    expect(new Set(after).size).toBe(after.length)
  })

  // Each fixture row is the ONLY row in its bucket, so an unconditional exclusion
  // deletes a header outright instead of merely de-duplicating one.
  it('keeps the header of a bucket whose SOLE row is hovered', () => {
    renderSidebar(SLOTS_INITIAL, FOLDERS)
    openFlatLane()
    const before = headers()
    expect(before).toHaveLength(3)

    // Nothing re-sorted, so B sits in its natural date position and opens its bucket.
    hover('chat-b')
    expect(headers()).toEqual(before)
  })

  it('keeps the first bucket header when the lane top row is hovered', () => {
    renderSidebar(SLOTS_INITIAL, FOLDERS)
    openFlatLane()
    const before = headers()
    expect(before).toHaveLength(3)

    hover('chat-a')
    expect(headers()).toEqual(before)
  })
})
