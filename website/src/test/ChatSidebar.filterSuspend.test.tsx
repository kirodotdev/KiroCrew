/**
 * Pause and resume every status filter at once, from the sort/filter menu.
 *
 * Looking at the whole list used to cost the filter setup: the only way to widen
 * it was to clear the chips, then re-pick each one from the menu. The menu now
 * carries a "Pause all filters" / "Resume all filters" row that lifts every
 * active status chip together and puts them all back. The chips stay in the row
 * while paused, marked with a Pause glyph and a dashed border, so the row still
 * says which filters are set.
 *
 * A click on a chip is unchanged: it CLEARS that filter, as it always has. The
 * pause is global, one control, in the menu. A menu row rather than a third
 * button in the chip row, because AUTOSDE `max-two-buttons-per-row` grandfathers
 * that row but forbids growing it.
 *
 * Persistence rides on the existing per-filter storage key: '0' off, '1' on,
 * '2' on but paused. No second key, so "is it on" and "is it paused" cannot
 * disagree.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { requestSlotReveal } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
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
// Legacy single-lane list (no tag columns) keeps the rows flat + easy to query.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue([]),
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
import type { ChatSlot } from '../types'

const UNREAD_KEY = 'mc-session-unread-only'
const PINNED_KEY = 'mc-session-pinned-only'
const RUNNING_KEY = 'mc-session-running-only'
const RECENT_KEY = 'mc-session-recent-only'
const FRESH = new Date(Date.now() - 60 * 1000).toISOString()

/** One unread, one pinned, one plain: each status filter keeps exactly one row. */
const SLOTS = [
  { key: 'u', title: 'unread session', running: false, messages: 2, last_turn_ts: FRESH },
  { key: 'p', title: 'pinned session', running: false, messages: 2, last_turn_ts: FRESH, pinned: true },
  { key: 'x', title: 'plain session', running: false, messages: 2, last_turn_ts: FRESH },
] as unknown as ChatSlot[]

function renderSidebar(slots: ChatSlot[] = SLOTS, unreadSlots: string[] = ['u']) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {}, automations: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  const tree = (unread: string[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={unread}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const view = render(tree(unreadSlots))
  /** Hand the sidebar a new unread list, the way an SSE frame would. */
  const setUnread = (next: string[]) => view.rerender(tree(next))
  return { ...view, store, setUnread }
}

/** Open the header's sort/filter dropdown. */
function openFilterMenu(utils: ReturnType<typeof renderSidebar>) {
  fireEvent.keyDown(utils.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

/** A chip that is kept but lifted carries the dashed border and the Pause glyph. */
function expectPaused(chip: HTMLElement, paused: boolean) {
  const classes = chip.className.split(/\s+/)
  if (paused) expect(classes).toContain('border-dashed')
  else expect(classes).not.toContain('border-dashed')
}

describe('chat sidebar: pause and resume all status filters from the menu', () => {
  it('"Pause all filters" lifts every active filter; the row then reads "Resume all filters" and puts them back', async () => {
    localStorage.setItem(UNREAD_KEY, '1')
    localStorage.setItem(PINNED_KEY, '1')
    const utils = renderSidebar()
    // Unread OR pinned: only the plain row is hidden.
    await waitFor(() => expect(utils.queryByText('plain session')).toBeNull())
    expectPaused(utils.getByTestId('filter-chip-unread'), false)

    openFilterMenu(utils)
    const pauseAll = await utils.findByTestId('filter-pause-all')
    expect(pauseAll).toHaveTextContent('Pause all filters')
    expect(pauseAll).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(pauseAll)

    // The list widens and both chips stay, marked.
    await waitFor(() => expect(utils.queryByText('plain session')).not.toBeNull())
    expectPaused(utils.getByTestId('filter-chip-unread'), true)
    expectPaused(utils.getByTestId('filter-chip-pinned'), true)
    expect(localStorage.getItem(UNREAD_KEY)).toBe('2')
    expect(localStorage.getItem(PINNED_KEY)).toBe('2')
    // Filters that were off stay off: no chip, nothing written.
    expect(utils.queryByTestId('filter-chip-running')).toBeNull()
    expect(localStorage.getItem(RUNNING_KEY)).toBeNull()
    expect(localStorage.getItem(RECENT_KEY)).toBeNull()

    // The menu stays open (preventDefault on select), and the row flipped.
    const resumeAll = await utils.findByTestId('filter-pause-all')
    expect(resumeAll).toHaveTextContent('Resume all filters')
    expect(resumeAll).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(resumeAll)
    await waitFor(() => expect(utils.queryByText('plain session')).toBeNull())
    expectPaused(utils.getByTestId('filter-chip-unread'), false)
    expect(localStorage.getItem(UNREAD_KEY)).toBe('1')
    expect(localStorage.getItem(PINNED_KEY)).toBe('1')
  })

  it('a paused filter survives a remount through the stored "2"', async () => {
    localStorage.setItem(UNREAD_KEY, '2')
    localStorage.setItem(PINNED_KEY, '2')
    const utils = renderSidebar()
    // Nothing is narrowed, yet the chips are there to resume from.
    expect(await utils.findByText('plain session')).not.toBeNull()
    expectPaused(utils.getByTestId('filter-chip-unread'), true)
    expectPaused(utils.getByTestId('filter-chip-pinned'), true)
    openFilterMenu(utils)
    expect(await utils.findByTestId('filter-pause-all')).toHaveTextContent('Resume all filters')
  })

  it('a MIXED "1" / "2" load reads as not paused and is rewritten to "1"', async () => {
    // Only a build that paused filters one at a time could store that mix. The
    // menu row has one state to offer, so the load settles on narrowing.
    localStorage.setItem(UNREAD_KEY, '2')
    localStorage.setItem(PINNED_KEY, '1')
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('plain session')).toBeNull())
    expectPaused(utils.getByTestId('filter-chip-unread'), false)
    expect(localStorage.getItem(UNREAD_KEY)).toBe('1')
    expect(localStorage.getItem(PINNED_KEY)).toBe('1')
    openFilterMenu(utils)
    expect(await utils.findByTestId('filter-pause-all')).toHaveTextContent('Pause all filters')
  })

  it('a filter turned on while paused JOINS the pause: stored "2", the list stays wide', async () => {
    localStorage.setItem(UNREAD_KEY, '2')
    const utils = renderSidebar()
    expect(await utils.findByText('plain session')).not.toBeNull()

    openFilterMenu(utils)
    fireEvent.click(await utils.findByRole('menuitem', { name: /^Pinned/ }))
    // The person asked for the whole list, so the new filter does not narrow it
    // on its own; one Resume brings every chip back together.
    await waitFor(() => expect(utils.queryByTestId('filter-chip-pinned')).not.toBeNull())
    expect(utils.queryByText('plain session')).not.toBeNull()
    expectPaused(utils.getByTestId('filter-chip-pinned'), true)
    expect(localStorage.getItem(PINNED_KEY)).toBe('2')
    expect(await utils.findByTestId('filter-pause-all')).toHaveTextContent('Resume all filters')
  })

  it('a chip click still CLEARS its filter, paused or not, and the last one leaving drops the pause', async () => {
    localStorage.setItem(UNREAD_KEY, '2')
    const utils = renderSidebar()
    expect(await utils.findByText('plain session')).not.toBeNull()
    const chip = utils.getByTestId('filter-chip-unread')
    // The accessible name is what the click does, exactly as on a chip that is
    // not paused: the pause never becomes a second meaning for the click.
    expect(chip).toHaveAccessibleName('Clear Unread filter')
    expect(chip).toHaveTextContent('Unread (1)')
    fireEvent.click(chip)
    await waitFor(() => expect(utils.queryByTestId('filter-chip-unread')).toBeNull())
    expect(localStorage.getItem(UNREAD_KEY)).toBe('0')

    // The pause went with the last chip: the next filter narrows.
    openFilterMenu(utils)
    fireEvent.click(await utils.findByRole('menuitem', { name: /^Unread/ }))
    await waitFor(() => expect(utils.queryByText('plain session')).toBeNull())
    expectPaused(utils.getByTestId('filter-chip-unread'), false)
    expect(localStorage.getItem(UNREAD_KEY)).toBe('1')
  })

  it('the pause row is absent with no active filter', async () => {
    const utils = renderSidebar()
    expect(await utils.findByText('plain session')).not.toBeNull()
    openFilterMenu(utils)
    await utils.findByRole('menuitem', { name: /^Unread/ })
    expect(utils.queryByTestId('filter-pause-all')).toBeNull()
  })

  it('the unread auto-drain leaves a PAUSED unread filter alone', async () => {
    // The drain exists so the person is not left staring at an empty list. A
    // paused filter hides nothing, and pausing means "keep it for later", so
    // loading with nothing unread must not throw the chip away.
    localStorage.setItem(UNREAD_KEY, '2')
    const utils = renderSidebar(SLOTS, [])
    expect(await utils.findByText('plain session')).not.toBeNull()
    const chip = await utils.findByTestId('filter-chip-unread')
    expectPaused(chip, true)
    expect(localStorage.getItem(UNREAD_KEY)).toBe('2')
  })

  it('the unread auto-drain still drops an ON unread filter that loads empty', async () => {
    // POSITIVE CONTROL for the test above: same load, filter not paused.
    localStorage.setItem(UNREAD_KEY, '1')
    const utils = renderSidebar(SLOTS, [])
    expect(await utils.findByText('plain session')).not.toBeNull()
    await waitFor(() => expect(utils.queryByTestId('filter-chip-unread')).toBeNull())
    expect(localStorage.getItem(UNREAD_KEY)).toBe('0')
  })

  it('an inbox that drains WHILE paused drains the filter on resume, not into an empty list', async () => {
    // The pause freezes the drain's count baseline instead of recording the
    // zero. Without that, resuming would narrow on an unread filter with nothing
    // unread left, and the drain would read 0 -> 0 as "nothing changed" forever.
    localStorage.setItem(UNREAD_KEY, '1')
    const utils = renderSidebar(SLOTS, ['u'])
    await waitFor(() => expect(utils.queryByText('plain session')).toBeNull())

    // The menu stays open across both clicks (preventDefault on select), so the
    // pause and the resume below are one visit to it.
    openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('filter-pause-all'))
    await waitFor(() => expect(utils.queryByText('plain session')).not.toBeNull())

    // The inbox drains while the filters are paused: the chip must stay, and the
    // drain must not record this zero as its new baseline.
    utils.setUnread([])
    await waitFor(() => expect(utils.getByTestId('filter-chip-unread')).toBeTruthy())
    expect(localStorage.getItem(UNREAD_KEY)).toBe('2')

    // Resume: the drain runs now, with the count the pause froze, so the filter
    // takes itself off instead of hiding every row.
    fireEvent.click(await utils.findByTestId('filter-pause-all'))
    await waitFor(() => expect(utils.queryByTestId('filter-chip-unread')).toBeNull())
    expect(localStorage.getItem(UNREAD_KEY)).toBe('0')
    expect(utils.queryByText('plain session')).not.toBeNull()
  })

  it('revealing a hidden session clears the status filters, and the cleared state carries no stale pause', async () => {
    // The reveal registry's clear drops every status chip so the target can
    // render. A pause cannot be in force here: a paused filter hides nothing, so
    // it is never what the reveal is clearing. What this pins is the other half
    // of that clear: it must leave no pause behind either, or the next filter
    // the person turns on would come back already lifted.
    const scrollIntoView = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollIntoView
    try {
      localStorage.setItem(UNREAD_KEY, '1')
      localStorage.setItem(PINNED_KEY, '1')
      const utils = renderSidebar()
      // Unread OR pinned: the plain row is the hidden target.
      await waitFor(() => expect(utils.queryByText('plain session')).toBeNull())

      utils.store.dispatch(requestSlotReveal('x'))
      await waitFor(() => expect(utils.queryByTestId('filter-chip-pinned')).toBeNull())
      expect(utils.queryByTestId('filter-chip-unread')).toBeNull()
      expect(localStorage.getItem(UNREAD_KEY)).toBe('0')
      expect(localStorage.getItem(PINNED_KEY)).toBe('0')

      openFilterMenu(utils)
      fireEvent.click(await utils.findByRole('menuitem', { name: /^Unread/ }))
      await waitFor(() => expect(utils.queryByText('plain session')).toBeNull())
      expectPaused(utils.getByTestId('filter-chip-unread'), false)
      expect(localStorage.getItem(UNREAD_KEY)).toBe('1')
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })
})
