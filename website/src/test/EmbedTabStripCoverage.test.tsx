/**
 * Coverage-focused tests for EmbedTabStrip — the tab bar the Kiro Crew IDE
 * plugin renders above the embedded chat.
 *
 * EmbedComponents.test.tsx already exercises the render + click paths, so this
 * file aims at the cold half of the component:
 *   - loadTabs() fallbacks (window injection, malformed storage, no active slot)
 *   - the shift+click "create a chat directly" mutation and its onSuccess
 *   - the plugin-pushed `kirocrew-tab-state` event landing on a Sessions tab
 *   - the activeSlot-sync effect (all four of its outcomes)
 *   - the slot-deletion effect collapsing back to a Sessions tab
 *   - keyboard activation, wheel scrolling, and the pointer drag-reorder
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

vi.mock('../store/chatSlice', async () => {
  const actual = await vi.importActual('../store/chatSlice')
  return { ...actual, createSlot: vi.fn(() => ({ unwrap: () => Promise.resolve({ key: 'new-slot' }) })) }
})

import EmbedTabStrip from '../components/EmbedTabStrip'
import { createSlot, setActiveSlot } from '../store/chatSlice'

const STORAGE_KEY = 'kirocrew-embed-tabs'
const STORAGE_INDEX_KEY = 'kirocrew-embed-active-index'

/** Tab state the host plugin injects onto `window`. */
interface EmbedTabsWindow extends Window {
  __kirocrewTabs?: string[]
  __kirocrewActiveTabIndex?: number
}
const tabsWindow = window as EmbedTabsWindow

type Slots = RootState['dashboard']['slots']

const DEFAULT_SLOTS = [
  { key: 'chat-1', title: 'First Chat' },
  { key: 'chat-2', title: 'Second Chat' },
  { key: 'chat-3', title: 'Third Chat' },
] as unknown as Slots

let queryClient: QueryClient

/**
 * Redux Toolkit REPLACES a preloaded slice rather than merging it with
 * initialState, so spread the real defaults before applying overrides —
 * otherwise reducers see a slice with missing keys.
 */
function makeStore(opts: { slots?: Slots; activeSlot?: string | null; unread?: string[] } = {}) {
  const defaults = createTestStore().getState()
  return createTestStore({
    dashboard: {
      ...defaults.dashboard,
      slots: opts.slots ?? DEFAULT_SLOTS,
      unreadSlots: opts.unread ?? [],
    },
    chat: { ...defaults.chat, activeSlot: opts.activeSlot ?? 'chat-1' },
  })
}

function wrap(store: ReturnType<typeof makeStore>) {
  const utils = render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/embed/chat/chat-1']}>
          <EmbedTabStrip />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
  return { ...utils, store }
}

function seed(tabs: string[], index = 0) {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(tabs))
  sessionStorage.setItem(STORAGE_INDEX_KEY, String(index))
}

function storedTabs(): string[] {
  return JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '[]') as string[]
}

function tabTitles(): string[] {
  return screen.getAllByRole('tab').map(el => el.textContent ?? '')
}

function makeRect(left: number, right: number): DOMRect {
  return {
    left, right, top: 0, bottom: 24, width: right - left, height: 24,
    x: left, y: 0, toJSON: () => ({}),
  } as DOMRect
}

/** The scroll container is the tabs' shared parent. */
function stripOf(tabs: HTMLElement[]): HTMLElement {
  return tabs[0].parentElement as HTMLElement
}

/**
 * happy-dom reports every rect as zero and treats scrollLeft as read-only, so
 * lay the tabs out at 100px each (mid-points 50 / 150 / 250) inside a 0..400
 * strip and give the strip a writable scroll offset. Without this the drop
 * target arithmetic and the edge auto-scroll cannot be observed at all.
 */
function layOut(tabs: HTMLElement[]) {
  const strip = stripOf(tabs)
  strip.getBoundingClientRect = () => makeRect(0, 400)
  tabs.forEach((el, i) => { el.getBoundingClientRect = () => makeRect(i * 100, i * 100 + 100) })
  Object.defineProperty(strip, 'scrollLeft', { value: 0, writable: true, configurable: true })
  return strip
}

/** Let the component's `setTimeout(..., 0)` persist callback run. */
async function flushTimers() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
}

beforeEach(() => {
  vi.clearAllMocks()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  sessionStorage.clear()
  delete tabsWindow.__kirocrewTabs
  delete tabsWindow.__kirocrewActiveTabIndex
})

afterEach(() => {
  sessionStorage.clear()
  delete tabsWindow.__kirocrewTabs
  delete tabsWindow.__kirocrewActiveTabIndex
})

describe('EmbedTabStrip — initial tab resolution', () => {
  it('adopts tabs injected on window by the host plugin', () => {
    tabsWindow.__kirocrewTabs = ['chat-2', 'chat-3']
    tabsWindow.__kirocrewActiveTabIndex = 1
    wrap(makeStore({ activeSlot: null }))
    expect(tabTitles()).toEqual(['Second Chat', 'Third Chat'])
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-3?sid=chat-3', { replace: true })
  })

  it('ignores malformed stored state and falls back to the active slot', () => {
    sessionStorage.setItem(STORAGE_KEY, 'not-json')
    wrap(makeStore({ activeSlot: 'chat-2' }))
    expect(tabTitles()).toEqual(['Second Chat'])
  })

  it('ignores an empty stored tab list and falls back to the active slot', () => {
    seed([], 0)
    wrap(makeStore({ activeSlot: 'chat-2' }))
    expect(tabTitles()).toEqual(['Second Chat'])
  })

  it('skips the mount navigation when the stored index points past the last tab', () => {
    seed(['chat-1'], 5)
    wrap(makeStore())
    expect(mockNavigate).not.toHaveBeenCalled()
  })
})

describe('EmbedTabStrip — shift+click creates a chat directly', () => {
  it('adds no tab when the created slot comes back without a key', async () => {
    vi.mocked(createSlot).mockReturnValueOnce(
      { unwrap: () => Promise.resolve({}) } as unknown as ReturnType<typeof createSlot>
    )
    seed(['chat-1'], 0)
    wrap(makeStore())
    fireEvent.click(screen.getByLabelText('New tab'), { shiftKey: true })
    await waitFor(() => expect(vi.mocked(createSlot)).toHaveBeenCalled())
    await flushTimers()
    expect(tabTitles()).toEqual(['First Chat'])
    expect(storedTabs()).toEqual(['chat-1'])
  })
})

describe('EmbedTabStrip — plugin-pushed tab state', () => {
  it('navigates to the sessions list when the pushed active tab is empty', () => {
    seed(['chat-1'], 0)
    wrap(makeStore())
    mockNavigate.mockClear()
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-tab-state', { detail: { tabs: ['', 'chat-2'], activeIndex: 0 } }))
    })
    expect(mockNavigate).toHaveBeenCalledWith('/embed/sessions')
    expect(tabTitles()).toEqual(['Sessions', 'Second Chat'])
    expect(storedTabs()).toEqual(['', 'chat-2'])
  })

  it('defaults the pushed active index to the first tab when it is not a number', () => {
    seed(['chat-1'], 0)
    wrap(makeStore())
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-tab-state', { detail: { tabs: ['chat-3', 'chat-2'] } }))
    })
    expect(sessionStorage.getItem(STORAGE_INDEX_KEY)).toBe('0')
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-3?sid=chat-3')
  })

  it('ignores a pushed payload whose tabs field is not an array', () => {
    seed(['chat-1'], 0)
    wrap(makeStore())
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-tab-state', { detail: { tabs: 'chat-2' } }))
    })
    expect(tabTitles()).toEqual(['First Chat'])
  })
})

describe('EmbedTabStrip — syncing with the active slot', () => {
  it('drops the Sessions tab when the chosen slot is already open elsewhere', () => {
    seed(['', 'chat-2'], 0)
    const { store } = wrap(makeStore())
    act(() => { store.dispatch(setActiveSlot('chat-2')) })
    expect(screen.queryByText('Sessions')).toBeNull()
    expect(tabTitles()).toEqual(['Second Chat'])
    expect(storedTabs()).toEqual(['chat-2'])
    expect(sessionStorage.getItem(STORAGE_INDEX_KEY)).toBe('0')
  })

  it('replaces the Sessions tab in place when a brand-new slot becomes active', () => {
    seed([''], 0)
    const { store } = wrap(makeStore())
    act(() => { store.dispatch(setActiveSlot('chat-3')) })
    expect(tabTitles()).toEqual(['Third Chat'])
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-3?sid=chat-3')
    expect(storedTabs()).toEqual(['chat-3'])
  })

  it('does nothing when the active slot already matches the active tab', () => {
    seed(['chat-1', 'chat-2'], 1)
    const { store } = wrap(makeStore())
    mockNavigate.mockClear()
    act(() => { store.dispatch(setActiveSlot('chat-2')) })
    expect(sessionStorage.getItem(STORAGE_INDEX_KEY)).toBe('1')
    expect(mockNavigate).not.toHaveBeenCalled()
  })

  it('does nothing when the active index has no tab behind it', () => {
    seed(['chat-1'], 4)
    const { store } = wrap(makeStore())
    act(() => { store.dispatch(setActiveSlot('chat-2')) })
    expect(tabTitles()).toEqual(['First Chat'])
    expect(mockNavigate).not.toHaveBeenCalled()
  })
})

describe('EmbedTabStrip — deleted slots', () => {
  it('collapses to a Sessions tab and navigates there when every chat tab is gone', () => {
    seed(['', 'chat-9'], 1)
    wrap(makeStore())
    expect(tabTitles()).toEqual(['Sessions'])
    expect(mockNavigate).toHaveBeenCalledWith('/embed/sessions')
    expect(storedTabs()).toEqual([''])
  })

  it('leaves an inactive tab list untouched when the deleted tab was not active', () => {
    seed(['chat-1', 'chat-9'], 0)
    wrap(makeStore())
    mockNavigate.mockClear()
    expect(tabTitles()).toEqual(['First Chat'])
    expect(mockNavigate).not.toHaveBeenCalledWith('/embed/sessions')
  })
})

describe('EmbedTabStrip — selection and scrolling', () => {
  it('navigates to the sessions list when the Sessions tab is clicked', () => {
    seed(['chat-1', ''], 0)
    wrap(makeStore())
    mockNavigate.mockClear()
    fireEvent.click(screen.getByText('Sessions'))
    expect(mockNavigate).toHaveBeenCalledWith('/embed/sessions')
    expect(sessionStorage.getItem(STORAGE_INDEX_KEY)).toBe('1')
  })

  it('activates a tab on Enter and on Space', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    fireEvent.keyDown(tabs[1], { key: 'Enter' })
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-2?sid=chat-2')
    mockNavigate.mockClear()
    fireEvent.keyDown(screen.getAllByRole('tab')[0], { key: ' ' })
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-1?sid=chat-1')
  })

  it('ignores other keys', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    mockNavigate.mockClear()
    fireEvent.keyDown(screen.getAllByRole('tab')[1], { key: 'a' })
    expect(mockNavigate).not.toHaveBeenCalled()
  })

  it('turns vertical wheel movement into horizontal scrolling', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const strip = layOut(screen.getAllByRole('tab'))
    fireEvent.wheel(strip, { deltaY: 120 })
    expect(strip.scrollLeft).toBe(120)
  })
})

describe('EmbedTabStrip — pointer drag reorder', () => {
  it('activates the tab the drag starts on', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    layOut(tabs)
    mockNavigate.mockClear()
    fireEvent.pointerDown(tabs[1], { clientX: 150, pointerId: 1 })
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-2?sid=chat-2')
    expect(sessionStorage.getItem(STORAGE_INDEX_KEY)).toBe('1')
  })

  it('activates the Sessions tab the drag starts on', () => {
    seed(['chat-1', ''], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    layOut(tabs)
    mockNavigate.mockClear()
    fireEvent.pointerDown(tabs[1], { clientX: 150, pointerId: 1 })
    expect(mockNavigate).toHaveBeenCalledWith('/embed/sessions')
  })

  it('does not lift the tab until the pointer travels past the threshold', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    const strip = layOut(tabs)
    fireEvent.pointerDown(tabs[0], { clientX: 50, pointerId: 1 })
    fireEvent.pointerMove(strip, { clientX: 54 })
    expect(screen.getAllByRole('tab')[0].style.transform).toBe('')
  })

  it('lifts the tab under the cursor once the drag threshold is crossed', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    const strip = layOut(tabs)
    fireEvent.pointerDown(tabs[0], { clientX: 50, pointerId: 1 })
    fireEvent.pointerMove(strip, { clientX: 120 })
    const after = screen.getAllByRole('tab')
    expect(after[0].style.transform).toBe('translateX(70px)')
    expect(after[0].style.cursor).toBe('grabbing')
    // Non-dragged tabs animate into their new slot instead of being offset.
    expect(after[1].style.transform).toBe('')
    expect(after[1].style.transition).toBe('transform 200ms ease')
  })

  it('scrolls the strip when the drag reaches either edge', () => {
    seed(['chat-1', 'chat-2', 'chat-3'], 1)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    const strip = layOut(tabs)
    fireEvent.pointerDown(tabs[1], { clientX: 150, pointerId: 1 })
    fireEvent.pointerMove(strip, { clientX: 380 })
    expect(strip.scrollLeft).toBe(8)
    fireEvent.pointerMove(strip, { clientX: 10 })
    expect(strip.scrollLeft).toBe(0)
    // Middle of the strip is outside both edge zones.
    fireEvent.pointerMove(strip, { clientX: 200 })
    expect(strip.scrollLeft).toBe(0)
  })

  it('ignores pointer movement when no drag is in flight', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const strip = layOut(screen.getAllByRole('tab'))
    fireEvent.pointerMove(strip, { clientX: 300 })
    expect(screen.getAllByRole('tab')[0].style.transform).toBe('')
  })

  it('moves a tab to the left when dropped before an earlier tab', async () => {
    seed(['chat-1', 'chat-2', 'chat-3'], 2)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    const strip = layOut(tabs)
    fireEvent.pointerDown(tabs[2], { clientX: 250, pointerId: 1 })
    fireEvent.pointerMove(strip, { clientX: 100 })
    fireEvent.pointerUp(strip, { clientX: 20 })
    expect(tabTitles()).toEqual(['Third Chat', 'First Chat', 'Second Chat'])
    await flushTimers()
    expect(storedTabs()).toEqual(['chat-3', 'chat-1', 'chat-2'])
    expect(sessionStorage.getItem(STORAGE_INDEX_KEY)).toBe('0')
  })

  it('keeps the order when the drop lands back on the original slot', async () => {
    seed(['chat-1', 'chat-2', 'chat-3'], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    const strip = layOut(tabs)
    fireEvent.pointerDown(tabs[0], { clientX: 50, pointerId: 1 })
    fireEvent.pointerMove(strip, { clientX: 80 })
    fireEvent.pointerUp(strip, { clientX: 80 })
    expect(tabTitles()).toEqual(['First Chat', 'Second Chat', 'Third Chat'])
    await flushTimers()
    expect(storedTabs()).toEqual(['chat-1', 'chat-2', 'chat-3'])
  })

  it('treats a press without movement as a plain click, not a reorder', async () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    const strip = layOut(tabs)
    fireEvent.pointerDown(tabs[0], { clientX: 50, pointerId: 1 })
    fireEvent.pointerUp(strip, { clientX: 52 })
    expect(tabTitles()).toEqual(['First Chat', 'Second Chat'])
    await flushTimers()
    expect(storedTabs()).toEqual(['chat-1', 'chat-2'])
  })

  it('clears the lifted state when the drag is cancelled mid-flight', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    const strip = layOut(tabs)
    fireEvent.pointerDown(tabs[0], { clientX: 50, pointerId: 1 })
    fireEvent.pointerMove(strip, { clientX: 120 })
    expect(screen.getAllByRole('tab')[0].style.transform).toBe('translateX(70px)')
    fireEvent.pointerCancel(strip)
    expect(screen.getAllByRole('tab')[0].style.transform).toBe('')
  })

  it('does not start a drag from the close button', () => {
    seed(['chat-1', 'chat-2'], 0)
    wrap(makeStore())
    const tabs = screen.getAllByRole('tab')
    const strip = layOut(tabs)
    mockNavigate.mockClear()
    fireEvent.pointerDown(screen.getAllByLabelText('Close tab')[1], { clientX: 190, pointerId: 1 })
    fireEvent.pointerMove(strip, { clientX: 300 })
    expect(screen.getAllByRole('tab')[1].style.transform).toBe('')
    expect(mockNavigate).not.toHaveBeenCalled()
  })
})
