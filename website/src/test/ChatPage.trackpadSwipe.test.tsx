/**
 * The chat page's desktop trackpad swipe on the right side panel, mirroring the
 * Mates page's (MembersPage.sidePanel.test.tsx): a two-finger run to the left
 * pulls the panel in under the fingers, to the right it follows them out; the
 * shell's column (and so the chat's right edge) is exactly as wide as the
 * panel's visible part; the panel is released only on a real lift-off, by the
 * phone's rule; a new run grabs a settle; reduced motion switches once; and a
 * horizontal scroller keeps its own scroll.
 */
import { describe, it, expect, beforeEach, afterEach, vi, onTestFinished } from 'vitest'
import { Profiler } from 'react'
import { render, act, waitFor, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs } from '../hooks/usePanelTabs'
import { setSidePanelDock } from '../hooks/useSidePanelDock'
import { switchSlot, openActivityPanel } from '../store/chatSlice'
import { swipeTiming, SWIPE_GESTURE_IDLE_MS } from '../hooks/useTrackpadSwipe'
import { MotionGlobalConfig, frame, frameData } from 'framer-motion'

// --- Stub child components (the ChatPage.responsivePanel scaffold) ---
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '800px', input: '816px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))
vi.mock('../pages/chat/SidePanel', () => ({
  default: () => <div data-testid="side-panel" />,
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}
    )])
  ),
}))

/** No query matches: a desktop pointer with motion allowed. */
const noMedia = ((q: string) => ({
  matches: false, media: q, onchange: null,
  addListener: vi.fn(), removeListener: vi.fn(),
  addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
})) as unknown as typeof window.matchMedia
Object.defineProperty(window, 'matchMedia', { writable: true, value: noMedia })
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import ChatPage from '../pages/ChatPage'

/** The panel's own width, stood in for (this test DOM has no layout). */
const PANEL = 400
/** The event time `swipe()` stamps on every event (see there). */
const SWIPE_AT = 1
/** The docked column's measured width. Wider than the panel here, so a close
 *  that measured anything but the column would show it. */
const COLUMN = 430

function renderChat(onCommit?: () => void) {
  const store = createTestStore()
  act(() => {
    store.dispatch(switchSlot.pending('req-swipe', 'slot-swipe'))
    store.dispatch(switchSlot.fulfilled({
      key: 'slot-swipe',
      messages: [{ role: 'assistant', content: 'hi', cls: '' }],
      running: false, hasMore: false, total: 1, queue: [],
    }, 'req-swipe', 'slot-swipe'))
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <Routes><Route path="/chat/:slug?" element={<Profiler id="chat" onRender={() => onCommit?.()}><ChatPage /></Profiler>} /></Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, ...utils }
}

describe('ChatPage — a two-finger trackpad swipe moves the right side panel on desktop', () => {
  let slot: HTMLDivElement
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: 1400 })
    localStorage.clear()
    __resetPanelTabs()
    // The shell's actbar column. Its parent (here the body) holds the chat too.
    slot = document.createElement('div')
    slot.id = 'activity-bar-slot'
    document.body.appendChild(slot)
    const real = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')!
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
      configurable: true,
      get(this: HTMLElement) { return this.dataset.testid === 'side-panel' ? PANEL : real.get!.call(this) },
    })
    onTestFinished(() => { Object.defineProperty(HTMLElement.prototype, 'offsetWidth', real) })
    // A test that waits on the screen between two halves of one swipe must not
    // read that wait as the fingers lifting.
    swipeTiming.idleMs = 400
  })
  afterEach(() => {
    swipeTiming.idleMs = SWIPE_GESTURE_IDLE_MS
    slot.remove()
    setSidePanelDock('right')
    window.matchMedia = noMedia
    document.documentElement.style.overscrollBehaviorX = ''
  })

  /** `n` wheel events of `dx` on `target`; returns how many the page took. */
  const swipe = (target: Element, dx: number, n = 12, dy = 0) => {
    let taken = 0
    act(() => {
      for (let i = 0; i < n; i++) {
        const ev = new WheelEvent('wheel', { deltaX: dx, deltaY: dy, bubbles: true, cancelable: true })
        // One event time for every call: a batch has no speed, so it never
        // commits mid-drag on speed (rule C); the tests that mean speed stamp it.
        Object.defineProperty(ev, 'timeStamp', { value: SWIPE_AT })
        target.dispatchEvent(ev)
        if (ev.defaultPrevented) taken++
      }
    })
    return taken
  }
  /** Wheel events at explicit event times, as a trackpad stamps them. */
  const wheelSeq = (target: Element, steps: Array<[dx: number, at: number]>) => act(() => {
    for (const [dx, at] of steps) {
      const ev = new WheelEvent('wheel', { deltaX: dx, deltaY: 0, bubbles: true, cancelable: true })
      Object.defineProperty(ev, 'timeStamp', { value: at })
      target.dispatchEvent(ev)
    }
  })
  const steps = (dx: number, n: number, t0: number, gap = 16): Array<[number, number]> =>
    Array.from({ length: n }, (_, i) => [dx, t0 + i * gap])
  const tailSteps = (dx: number, n: number, t0: number, ratio = 0.85): Array<[number, number]> =>
    Array.from({ length: n }, (_, i) => [dx * ratio ** (i + 1), t0 + (i + 1) * 16])

  const column = () => screen.queryByTestId('side-panel-column')
  const sliding = () => column()?.getAttribute('data-sliding') === 'true'
  /** The column's width during a drag: the panel's visible part, px. */
  const spacerW = () => {
    const el = screen.queryByTestId('side-panel-swipe-spacer') as HTMLElement | null
    return el ? parseFloat(el.style.width || '0') : null
  }
  /** Somewhere in the chat pane. */
  const chat = (container: HTMLElement) => container.firstElementChild ?? container
  /** Open the panel, with its docked column as wide as the panel. */
  const openPanel = async (store: ReturnType<typeof renderChat>['store']) => {
    act(() => { store.dispatch(openActivityPanel()) })
    const col = await screen.findByTestId('side-panel-column')
    col.getBoundingClientRect = () => ({ width: COLUMN, height: 800, top: 0, left: 1400 - COLUMN, right: 1400, bottom: 800, x: 1400 - COLUMN, y: 0, toJSON: () => ({}) }) as DOMRect
    return col
  }

  it('a leftward run pulls the closed panel in under the fingers, the column as wide as its visible part, then settles open', async () => {
    const { store, container } = renderChat()
    expect(column()).toBeNull()
    // Fingers moving left: a trackpad reports a positive deltaX.
    expect(swipe(chat(container), 20, 5)).toBe(5)
    expect(sliding()).toBe(true)
    expect(slot.contains(screen.getByTestId('side-panel'))).toBe(true)
    await waitFor(() => expect(spacerW()).toBeCloseTo(100, 0))
    swipe(chat(container), 20, 5)
    await waitFor(() => expect(spacerW()).toBeCloseTo(200, 0))
    // Past the share, then the fingers lift: it settles open, and the column
    // takes the panel back with no spacer.
    await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(spacerW()).toBeNull()
    expect(screen.getByTestId('side-panel')).toBeInTheDocument()
  })

  it("a rightward run on the open panel: the column starts at the docked width and gives the chat the panel's width back as it goes", async () => {
    const { store, container } = renderChat()
    await openPanel(store)
    expect(swipe(chat(container), -20, 5)).toBe(5)
    expect(sliding()).toBe(true)
    // From the docked column's own width, never the window's or a re-measure:
    // the chat does not jump.
    await waitFor(() => expect(spacerW()).toBeCloseTo(COLUMN - 100, 0))
    swipe(chat(container), -20, 10)
    await waitFor(() => expect(column()).toBeNull(), { timeout: 3000 })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a slow 15% drag that lifts off falls back closed', async () => {
    const { store, container } = renderChat()
    const n = Math.round((PANEL * 0.15) / 3)
    const t0 = performance.now()
    wheelSeq(chat(container), steps(3, n, t0))
    await waitFor(() => expect(spacerW()).toBeCloseTo(3 * n, 0))
    await waitFor(() => expect(column()).toBeNull(), { timeout: 3000 })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a drag in progress re-renders nothing: each wheel event only moves the panel', async () => {
    let commits = 0
    const { store, container } = renderChat(() => { commits += 1 })
    await openPanel(store)
    // The run's first events start the drag: that render swaps the column.
    swipe(chat(container), -4, 2)
    expect(sliding()).toBe(true)
    // Startup is done once the column is the sliding window and shows the
    // drag's first offset.
    await waitFor(() => {
      expect(column()).toHaveAttribute('data-native-overlay')
      expect(spacerW()).toBeCloseTo(COLUMN - 8, 0)
    })
    commits = 0
    // One event per act, so React could not batch a state write per event
    // into one commit: it would show as twenty.
    for (let i = 0; i < 20; i++) swipe(chat(container), -4, 1)
    await waitFor(() => expect(spacerW()).toBeCloseTo(COLUMN - 88, 0))
    // The offset is a motion value the column reads frame by frame: no React
    // state per event. A few commits the page makes on its own (its data
    // settling) may land in the window; state per event would be twenty.
    expect(commits).toBeLessThanOrEqual(4)
  })

  it('a slide marks the page while it runs, and the mark goes when the panel rests', async () => {
    const { store, container } = renderChat()
    await openPanel(store)
    expect(document.documentElement.hasAttribute('data-panel-sliding')).toBe(false)
    swipe(chat(container), -4, 3)
    expect(sliding()).toBe(true)
    // Held work (utils/panelSlide): hover over the transcript, the pane width.
    expect(document.documentElement.hasAttribute('data-panel-sliding')).toBe(true)
    await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
    expect(document.documentElement.hasAttribute('data-panel-sliding')).toBe(false)
  })

  it('a short rightward run on the open panel falls back open, the same panel throughout', async () => {
    const { store, container } = renderChat()
    await openPanel(store)
    const panel = screen.getByTestId('side-panel')
    swipe(chat(container), -4, 5)
    expect(sliding()).toBe(true)
    // The drag never remounts the panel: its tabs (terminals, frames) live on.
    expect(screen.getByTestId('side-panel')).toBe(panel)
    // A native Browser view cannot follow a position-only move: the column is
    // marked as covering it for the whole drag and settle (useNativeBrowser).
    expect(column()).toHaveAttribute('data-native-overlay')
    await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(screen.getByTestId('side-panel')).toBe(panel)
    // Docked: the mark is gone, so the view shows again at the docked rect.
    expect(column()).not.toHaveAttribute('data-native-overlay')
  })

  it("in the desktop app, the shell's real lift-off releases the drag: a pause with the fingers down holds it", async () => {
    // The macOS shell's finger phases (lib/scrollTouch, through the preload).
    let emit: ((p: unknown) => void) | null = null
    ;(window as Window & { electronAPI?: unknown }).electronAPI = {
      onScrollTouch: (cb: (p: unknown) => void) => { emit = cb; return () => { emit = null } },
    }
    onTestFinished(() => { delete (window as Window & { electronAPI?: unknown }).electronAPI })
    const { store, container } = renderChat()
    // The reader's timers and frames on fake timers; the settle on framer's
    // manual clock (as in the grab test).
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    frameData.timestamp = performance.now()
    MotionGlobalConfig.useManualTiming = true
    onTestFinished(() => {
      MotionGlobalConfig.useManualTiming = false
      vi.useRealTimers()
    })
    const paint = (ms = 0) => act(async () => {
      frameData.timestamp += ms
      await new Promise<void>((resolve) => { frame.postRender(() => resolve()) })
    })
    act(() => { emit!('begin') })
    // 30% in (120px of 400), slowly, then the fingers rest on the pad.
    wheelSeq(chat(container), steps(4, 30, performance.now()))
    act(() => { vi.advanceTimersByTime(30 * 16) })
    // 600ms: past the gap that ends a drag in a browser, still the same drag.
    act(() => {
      vi.advanceTimersByTime(600)
      vi.advanceTimersToNextFrame()
    })
    await paint()
    expect(sliding()).toBe(true)
    expect(spacerW()).toBeCloseTo(120, 0)
    // The fingers lift: released one frame later, with no speed (they had
    // rested), past the share, so it settles open.
    act(() => { emit!('end') })
    act(() => { vi.advanceTimersToNextFrame() })
    for (let i = 0; i < 120 && sliding(); i++) await paint(1000 / 60)
    expect(sliding()).toBe(false)
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('a light partial close that commits settles front-loaded within 150ms: most of the way in the first frames, then only slower', async () => {
    const { store, container } = renderChat()
    await openPanel(store)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    frameData.timestamp = performance.now()
    MotionGlobalConfig.useManualTiming = true
    onTestFinished(() => {
      MotionGlobalConfig.useManualTiming = false
      vi.useRealTimers()
    })
    const paint = (ms = 0) => act(async () => {
      frameData.timestamp += ms
      await new Promise<void>((resolve) => { frame.postRender(() => resolve()) })
    })
    // 30% out (129px of 430), slowly, then the fingers stop and lift (a gap):
    // the release reads no speed, and past the share it commits to closed.
    wheelSeq(chat(container), steps(-3, 43, performance.now()))
    await paint()
    act(() => {
      vi.advanceTimersByTime(swipeTiming.idleMs)
      vi.advanceTimersToNextFrame()
    })
    // Frame by frame: the width the chat gets back, until the column is gone.
    const seen: number[] = [spacerW() ?? 0]
    let frames = 0
    while (column() && frames < 60) {
      await paint(1000 / 60)
      frames += 1
      const w = spacerW()
      if (w !== null) seen.push(w)
    }
    expect(store.getState().chat.activityOpen).toBe(false)
    // Within the trackpad settle's 150ms (9 frames, one more for the hand-back).
    expect(frames).toBeLessThanOrEqual(10)
    // Front-loaded: the first frame covers the most, and every step after is
    // smaller (1px of slack for framer's own sampling). No speed-up, no glide.
    const stepsPx = seen.slice(1).map((w, i) => seen[i] - w)
    const total = stepsPx.reduce((a, b) => a + b, 0)
    expect(stepsPx[0]).toBeGreaterThan(total * 0.4)
    for (let i = 1; i < stepsPx.length; i++) expect(stepsPx[i]).toBeLessThanOrEqual(stepsPx[i - 1] + 1)
    // Most of the way within the first three frames.
    expect(stepsPx.slice(0, 3).reduce((a, b) => a + b, 0)).toBeGreaterThan(total * 0.85)
  })

  /** Both clocks under the test's hand (as in the grab test). */
  const manualClocks = () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    frameData.timestamp = performance.now()
    MotionGlobalConfig.useManualTiming = true
    onTestFinished(() => {
      MotionGlobalConfig.useManualTiming = false
      vi.useRealTimers()
    })
    return (ms = 0) => act(async () => {
      frameData.timestamp += ms
      await new Promise<void>((resolve) => { frame.postRender(() => resolve()) })
    })
  }

  it('rule C: a fast drag commits before any lift-off and settles open within 150ms', async () => {
    const { store, container } = renderChat()
    const paint = manualClocks()
    // 10px per 16ms (0.6 px/ms): past the commit speed after three steps.
    wheelSeq(chat(container), steps(10, 4, performance.now()))
    // No gap has passed and no lift came: it is already settling.
    let frames = 0
    while (column()?.getAttribute('data-sliding') === 'true' && frames < 60) { await paint(1000 / 60); frames += 1 }
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(frames).toBeLessThanOrEqual(10)
  })

  it('a light swipe whose momentum creeps releases at the third tiny step, and its projected landing decides', async () => {
    const { store, container } = renderChat()
    await openPanel(store)
    const paint = manualClocks()
    const t0 = performance.now()
    // 48px out at 0.33 px/ms (under the commit speed), then a 2/1/2px creep.
    // Projected 120ms on, it lands past the 20% share: it closes, at once.
    wheelSeq(chat(container), [...steps(-16, 3, t0, 48), [-2, t0 + 112], [-1, t0 + 128], [-2, t0 + 144]])
    await paint()
    expect(spacerW()).toBeCloseTo(COLUMN - 48, 0)
    // No gap, no lift: the creep alone ends the drag, and the settle starts at once.
    let frames = 0
    while (sliding() && frames < 60) { await paint(1000 / 60); frames += 1 }
    expect(frames).toBeLessThanOrEqual(10)
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('no cooldown: a swipe back during the open momentum, within 150ms, closes it again', async () => {
    const { store, container } = renderChat()
    const paint = manualClocks()
    const t0 = performance.now()
    // A fast open (commits mid-drag), its momentum decaying, then the fingers
    // come straight back the other way: no gap anywhere.
    wheelSeq(chat(container), [
      ...steps(10, 4, t0),
      ...tailSteps(10, 4, t0 + 3 * 16),
      ...steps(-10, 4, t0 + 8 * 16),
    ])
    for (let i = 0; i < 60 && column(); i++) await paint(1000 / 60)
    expect(column()).toBeNull()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('no cooldown: fingers that turn straight back after a fast open, with no momentum between, close it', async () => {
    const { store, container } = renderChat()
    const paint = manualClocks()
    const t0 = performance.now()
    wheelSeq(chat(container), [...steps(10, 6, t0), ...steps(-10, 4, t0 + 6 * 16)])
    for (let i = 0; i < 60 && column(); i++) await paint(1000 / 60)
    expect(column()).toBeNull()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('rule C: a slow drag past 20% waits for the release, and dragged back under it falls back', async () => {
    const { store, container } = renderChat()
    const paint = manualClocks()
    const t0 = performance.now()
    // 120px in (30%) at 0.19 px/ms: past the share, and still following.
    wheelSeq(chat(container), steps(3, 40, t0))
    await paint()
    expect(sliding()).toBe(true)
    expect(spacerW()).toBeCloseTo(120, 0)
    // Back to 60px (15%), slowly, in the same run.
    wheelSeq(chat(container), steps(-3, 20, t0 + 40 * 16))
    await paint()
    expect(spacerW()).toBeCloseTo(60, 0)
    // Released under the share with no speed: it falls back closed.
    act(() => {
      vi.advanceTimersByTime(swipeTiming.idleMs)
      vi.advanceTimersToNextFrame()
    })
    for (let i = 0; i < 60 && column(); i++) await paint(1000 / 60)
    expect(column()).toBeNull()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a fast 10% flick with a momentum tail opens the panel', async () => {
    const { store, container } = renderChat()
    const per = Math.round((PANEL * 0.1) / 4)
    const t0 = performance.now()
    wheelSeq(chat(container), [...steps(per, 4, t0), ...tailSteps(per, 12, t0 + 3 * 16)])
    await waitFor(() => expect(sliding()).toBe(false), { timeout: 3000 })
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('a new run grabs an opening settle mid-way and follows from that offset', async () => {
    const { store, container } = renderChat()
    // Two clocks, both this test's, so the frame the second run lands in is a
    // chosen one, never one a stalled worker happens to paint:
    // - the swipe reader's quiet timer and its frame: fake timers;
    // - the settle: framer's manual timing, where animation time moves only
    //   when `frameData.timestamp` does. (Framer took its frame scheduler at
    //   import, so fake timers do not reach it; it still paints each batch.)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    // Start the manual clock where the real one stands, so an animation that
    // read the real clock before the switch is not thrown across the timeline.
    frameData.timestamp = performance.now()
    MotionGlobalConfig.useManualTiming = true
    onTestFinished(() => {
      MotionGlobalConfig.useManualTiming = false
      vi.useRealTimers()
    })
    /** Move animation time by `ms`, then let framer paint that frame. */
    const paint = (ms = 0) => act(async () => {
      frameData.timestamp += ms
      await new Promise<void>((resolve) => { frame.postRender(() => resolve()) })
    })
    /** The fingers lift: the quiet gap, then the frame its check waits for. */
    const lift = () => act(() => {
      vi.advanceTimersByTime(swipeTiming.idleMs)
      vi.advanceTimersToNextFrame()
    })
    let t = performance.now()
    // 240px in (60%), slowly, lifted with no speed: it settles open.
    wheelSeq(chat(container), steps(20, 12, t, 80))
    t += 12 * 80
    await paint()
    expect(spacerW()).toBeCloseTo(240, 0)
    lift()
    await paint()
    // Animation time has not moved, so neither has the panel.
    expect(spacerW()).toBeCloseTo(240, 0)
    // One frame into the settle.
    await paint(1000 / 60)
    const mid = spacerW() ?? 0
    expect(mid).toBeGreaterThan(240)
    expect(mid).toBeLessThan(PANEL)
    expect(sliding()).toBe(true)
    // A rightward run, in two halves, slowly: it stops the settle and follows
    // 1:1 from where it was. (Input lands in a frame not yet painted, so the
    // first half may still carry one frame of the settle.)
    frameData.timestamp += 1000 / 60
    wheelSeq(chat(container), steps(-20, 5, t, 80))
    t += 5 * 80
    await paint()
    const first = spacerW() ?? 0
    expect(first).toBeLessThanOrEqual(PANEL - 100 + 1)
    frameData.timestamp += 1000 / 60
    wheelSeq(chat(container), steps(-20, 10, t, 80))
    await paint()
    expect(first - (spacerW() ?? 0)).toBeCloseTo(200, 0)
    // Past the share toward closed, lifted with no speed: it closes.
    lift()
    for (let i = 0; i < 120 && column(); i++) await paint(1000 / 60)
    expect(column()).toBeNull()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it("a session switch mid-settle retires the settle: it never closes the next session's panel", async () => {
    const { store, container } = renderChat()
    /** Pick a session the way the sidebar does: the page stays mounted. */
    const pick = (key: string) => act(() => {
      store.dispatch(switchSlot.pending(`req-${key}`, key))
      store.dispatch(switchSlot.fulfilled({
        key, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        running: false, hasMore: false, total: 1, queue: [],
      }, `req-${key}`, key))
    })
    // Both sessions have the panel open.
    await openPanel(store)
    pick('slot-b')
    act(() => { store.dispatch(openActivityPanel()) })
    pick('slot-swipe')
    expect(store.getState().chat.activityOpen).toBe(true)
    await openPanel(store)
    // The same two clocks as the grab test, so the switch lands mid-settle.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    frameData.timestamp = performance.now()
    MotionGlobalConfig.useManualTiming = true
    onTestFinished(() => {
      MotionGlobalConfig.useManualTiming = false
      vi.useRealTimers()
    })
    const paint = (ms = 0) => act(async () => {
      frameData.timestamp += ms
      await new Promise<void>((resolve) => { frame.postRender(() => resolve()) })
    })
    // Swipe this session's panel closed: 300px of 400, lifted, settling out.
    wheelSeq(chat(container), steps(-20, 15, performance.now()))
    await paint()
    act(() => {
      vi.advanceTimersByTime(swipeTiming.idleMs)
      vi.advanceTimersToNextFrame()
    })
    await paint(1000 / 60)
    expect(sliding()).toBe(true)
    // Session B, before that settle has landed.
    pick('slot-b')
    const retiredAtOnce = !sliding()
    // Let the settle's time run out: nothing of it may reach B.
    for (let i = 0; i < 60; i++) await paint(1000 / 60)
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(localStorage.getItem('mc-activity-open:slot-b')).toBe('true')
    expect(column()).not.toBeNull()
    // And B's panel was handed back to its column on the switch itself.
    expect(retiredAtOnce).toBe(true)
    // The swiped session keeps the state it had: the close never landed.
    pick('slot-swipe')
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('closing with the shortcut mid-settle wins: the settle back open is retired, not landed', async () => {
    const { store, container } = renderChat()
    await openPanel(store)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    frameData.timestamp = performance.now()
    MotionGlobalConfig.useManualTiming = true
    onTestFinished(() => {
      MotionGlobalConfig.useManualTiming = false
      vi.useRealTimers()
    })
    const paint = (ms = 0) => act(async () => {
      frameData.timestamp += ms
      await new Promise<void>((resolve) => { frame.postRender(() => resolve()) })
    })
    // A short rightward run (40px of 400), lifted: it settles back open.
    wheelSeq(chat(container), steps(-4, 10, performance.now()))
    await paint()
    act(() => {
      vi.advanceTimersByTime(swipeTiming.idleMs)
      vi.advanceTimersToNextFrame()
    })
    await paint(1000 / 60)
    expect(sliding()).toBe(true)
    // The person closes the panel with the shortcut before it lands.
    act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })
    expect(store.getState().chat.activityOpen).toBe(false)
    // One frame for the closed panel's column to leave (it exits as drawn).
    await paint(1000 / 60)
    const retiredAtOnce = !sliding()
    // The settle's time runs out: it must not reopen the panel.
    for (let i = 0; i < 60; i++) await paint(1000 / 60)
    expect(store.getState().chat.activityOpen).toBe(false)
    expect(localStorage.getItem('mc-activity-open:slot-swipe')).toBe('false')
    expect(retiredAtOnce).toBe(true)
  })

  it('under reduced motion, toggling the panel before the run ends drops the swipe', () => {
    window.matchMedia = ((q: string) => ({
      matches: q.includes('prefers-reduced-motion'), media: q, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia
    const { store, container } = renderChat()
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    onTestFinished(() => { vi.useRealTimers() })
    // A clear 160px swipe to the left: it would open the panel at the run's end.
    expect(swipe(chat(container), 20, 8)).toBe(8)
    // The person opens it, then closes it again, with the shortcut.
    act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })
    act(() => { window.dispatchEvent(new Event('toggle-activity-panel')) })
    expect(store.getState().chat.activityOpen).toBe(false)
    act(() => {
      vi.advanceTimersByTime(swipeTiming.idleMs)
      vi.advanceTimersToNextFrame()
    })
    // The run's outcome came before the person's last word: dropped.
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it("under reduced motion, a session switch before the run ends drops the swipe: it never closes the next session's panel", async () => {
    window.matchMedia = ((q: string) => ({
      matches: q.includes('prefers-reduced-motion'), media: q, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia
    const { store, container } = renderChat()
    const pick = (key: string) => act(() => {
      store.dispatch(switchSlot.pending(`req-${key}`, key))
      store.dispatch(switchSlot.fulfilled({
        key, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        running: false, hasMore: false, total: 1, queue: [],
      }, `req-${key}`, key))
    })
    // Both sessions have the panel open.
    await openPanel(store)
    pick('slot-b')
    act(() => { store.dispatch(openActivityPanel()) })
    pick('slot-swipe')
    expect(store.getState().chat.activityOpen).toBe(true)
    // The run ends on the quiet timer and its frame: stepped by hand.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    onTestFinished(() => { vi.useRealTimers() })
    // A clear 160px swipe to the right on this session: no live drag.
    expect(swipe(chat(container), -20, 8)).toBe(8)
    expect(sliding()).toBe(false)
    // Session B, before the run has ended.
    pick('slot-b')
    act(() => {
      vi.advanceTimersByTime(swipeTiming.idleMs)
      vi.advanceTimersToNextFrame()
    })
    // The swipe was about the other session: B keeps its panel.
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(localStorage.getItem('mc-activity-open:slot-b')).toBe('true')
    // And the swiped session is left as it was.
    pick('slot-swipe')
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  /** A horizontal scroller in the chat with 600px to scroll, at `scrollLeft`. */
  const scroller = (container: HTMLElement, dir: 'ltr' | 'rtl', scrollLeft: number) => {
    const el = document.createElement('div')
    el.style.overflowX = 'auto'
    el.style.direction = dir
    Object.defineProperty(el, 'scrollWidth', { value: 900 })
    Object.defineProperty(el, 'clientWidth', { value: 300 })
    Object.defineProperty(el, 'scrollLeft', { value: scrollLeft })
    chat(container).appendChild(el)
    return el
  }

  it('leaves a run over a horizontal scroller with room to it', () => {
    const { container } = renderChat()
    // Room to scroll on (deltaX > 0 raises scrollLeft).
    expect(swipe(scroller(container, 'ltr', 0), 20, 5)).toBe(0)
    expect(column()).toBeNull()
  })

  it('reads a right-to-left scroller at its start (scrollLeft 0) as having room for a negative deltaX', async () => {
    const { store, container } = renderChat()
    await openPanel(store)
    expect(swipe(scroller(container, 'rtl', 0), -20, 5)).toBe(0)
    expect(sliding()).toBe(false)
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('is not bound while the panel docks at the bottom', () => {
    setSidePanelDock('bottom')
    const { store, container } = renderChat()
    expect(swipe(chat(container), 20, 12)).toBe(0)
    expect(sliding()).toBe(false)
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  /** A relaying frame (an artifact built with `relayWheel`) with its own
   *  stand-in window: the test DOM gives every frame a null `contentWindow`. */
  const relayFrame = (parent: Element) => {
    const f = document.createElement('iframe')
    f.setAttribute('data-wheel-relay', '')
    Object.defineProperty(f, 'contentWindow', { value: { postMessage: () => {} } })
    parent.appendChild(f)
    return f
  }
  /** `n` relayed wheels from `f`, one event time (no speed, as `swipe()`). */
  const relay = (f: HTMLIFrameElement, dx: number, n: number) => act(() => {
    for (let i = 0; i < n; i++) {
      const ev = new MessageEvent('message', { data: { type: 'mc-frame-wheel', dx, dy: 0, mode: 0 } })
      Object.defineProperty(ev, 'source', { value: f.contentWindow })
      Object.defineProperty(ev, 'timeStamp', { value: SWIPE_AT })
      window.dispatchEvent(ev)
    }
  })

  it("a rightward swipe relayed from an artifact frame in the open panel drags it shut", async () => {
    const { store } = renderChat()
    const col = await openPanel(store)
    const f = relayFrame(col)
    relay(f, -20, 5)
    expect(sliding()).toBe(true)
    await waitFor(() => expect(spacerW()).toBeCloseTo(COLUMN - 100, 0))
    relay(f, -20, 15)
    await waitFor(() => expect(column()).toBeNull(), { timeout: 3000 })
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('a frame outside the panel, or in a tab not on screen, cannot drive it; nor a leftward relayed run', async () => {
    const { store, container } = renderChat()
    const col = await openPanel(store)
    // A frame in the chat (a widget), not the panel.
    const stranger = relayFrame(chat(container))
    relay(stranger, -20, 20)
    expect(sliding()).toBe(false)
    // A frame in a tab kept mounted behind the one on screen.
    const hiddenTab = document.createElement('div')
    hiddenTab.style.display = 'none'
    col.appendChild(hiddenTab)
    relay(relayFrame(hiddenTab), -20, 20)
    expect(sliding()).toBe(false)
    // The shown frame, but toward opening: a relayed run may only close.
    relay(relayFrame(col), 20, 20)
    expect(sliding()).toBe(false)
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('arms only a marked frame in the panel, and only while the swipe listens', async () => {
    const { store } = renderChat()
    const col = await openPanel(store)
    const said: Array<{ type: string; on: boolean }> = []
    const f = document.createElement('iframe')
    f.setAttribute('data-wheel-relay', '')
    Object.defineProperty(f, 'contentWindow', { value: { postMessage: (m: { type: string; on: boolean }) => { said.push(m) } } })
    col.appendChild(f)
    // The frame says hello when it loads; the listening host arms it.
    act(() => {
      const ev = new MessageEvent('message', { data: { type: 'mc-frame-wheel-hello' } })
      Object.defineProperty(ev, 'source', { value: f.contentWindow })
      window.dispatchEvent(ev)
    })
    expect(said.at(-1)).toEqual({ type: 'mc-frame-wheel-armed', on: true })
    // A bottom dock turns the swipe off: the frame is told to stop.
    act(() => { setSidePanelDock('bottom') })
    await waitFor(() => expect(said.at(-1)).toEqual({ type: 'mc-frame-wheel-armed', on: false }))
  })

  it.each([
    ['role="dialog"', (el: HTMLElement) => el.setAttribute('role', 'dialog')],
    ['role="alertdialog"', (el: HTMLElement) => el.setAttribute('role', 'alertdialog')],
    ['aria-modal', (el: HTMLElement) => el.setAttribute('aria-modal', 'true')],
  ])('a swipe that starts inside a modal layer (%s) does nothing to the panel', (_name, mark) => {
    const { store, container } = renderChat()
    // A dialog drawn inside the shell, over the chat (the What's New overlay).
    const dialog = document.createElement('div')
    mark(dialog)
    const text = document.createElement('p')
    dialog.appendChild(text)
    chat(container).appendChild(dialog)
    expect(swipe(text, 20, 30)).toBe(0)
    expect(column()).toBeNull()
    expect(store.getState().chat.activityOpen).toBe(false)
  })

  it('leaves a rightward run with the panel closed to the browser', () => {
    const { container } = renderChat()
    expect(swipe(chat(container), -20, 12)).toBe(0)
    expect(column()).toBeNull()
  })

  it('under reduced motion there is no live drag: a clear swipe switches once at the end', async () => {
    window.matchMedia = ((q: string) => ({
      matches: q.includes('prefers-reduced-motion'), media: q, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia
    const { store, container } = renderChat()
    swipe(chat(container), 20, 8)
    expect(sliding()).toBe(false)
    expect(store.getState().chat.activityOpen).toBe(false)
    await waitFor(() => expect(store.getState().chat.activityOpen).toBe(true), { timeout: 3000 })
    expect(sliding()).toBe(false)
  })
})
