/**
 * An open side panel across a fold or an unfold.
 *
 * On a book-style foldable the same tab switches between the phone form (the
 * outer display, 382 px) and the desktop form (the inner display in landscape,
 * 951 px). The desktop form draws the side panel in the shell's activity column
 * and the phone form draws it as an overlay over the chat. When each surface
 * rendered its own panel, every switch unmounted the panel and mounted a new
 * one, so whatever the panel held only in its own state, such as an answer
 * typed into a Dashboard tab's question card, was gone after a fold or an
 * unfold.
 *
 * The stub below stands where SidePanel renders and holds a draft in its own
 * state, as those cards do. The real useIsMobile reads a live width, so each
 * flip is a resize the page answers the way a device answers a fold.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { render, act, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { sseSlots, sseConnected } from '../store/dashboardSlice'
import { setActiveSlot, openActivityPanel, closeActivityPanel } from '../store/chatSlice'
import { sidePanelIn } from '../utils/sidePanelHealth'
import { modelBrowserScroll } from './browserScrollModel'

/** How many times the stub panel has mounted and unmounted, and whether it
 *  was on the page at each mount. */
const panel = vi.hoisted(() => ({ mounts: 0, unmounts: 0, onPageAtMount: [] as boolean[] }))
/** Completion callbacks of the phone overlay's slides, run by hand. */
const pendingSettles = vi.hoisted(() => [] as (() => void)[])

vi.mock('framer-motion', async (importOriginal) => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  return {
    ...actual,
    animate: (_v: unknown, _to: unknown, opts?: { onComplete?: () => void }) => {
      if (opts?.onComplete) pendingSettles.push(opts.onComplete)
      return { stop: () => {} }
    },
  }
})
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '800px', input: '816px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))
vi.mock('../pages/chat/SidePanel', () => ({
  default: function DraftHoldingPanel() {
    const [draft, setDraft] = useState('')
    const root = useRef<HTMLDivElement>(null)
    const scroller = useRef<HTMLDivElement>(null)
    // A real panel's first effects scroll to the newest activity, reveal the
    // active tab and focus a terminal, and each does nothing off the page.
    useLayoutEffect(() => {
      panel.onPageAtMount.push(!!root.current?.isConnected)
      // Its box keeps an offset only in place, as a browser's does: the place
      // is the surface slot holding the panel's host.
      modelBrowserScroll(scroller.current!, () => root.current?.parentNode?.parentNode)
    }, [])
    useEffect(() => { panel.mounts++; return () => { panel.unmounts++ } }, [])
    return <div ref={root} data-testid="side-panel-root"><div ref={scroller} data-testid="panel-scroller" /><textarea aria-label="Answer" value={draft} onChange={e => setDraft(e.target.value)} /></div>
  },
  SIDE_PANEL_MIN_W: 320,
  SIDE_PANEL_RESERVED_W: 560,
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
}))
// The on-screen check closes a panel whose box has no size, and nothing here
// is laid out: a slow run would see it close the panel mid-test.
vi.mock('../hooks/useSidePanelHealth', () => ({ useSidePanelHealth: () => {} }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => {
  const AGENTS = { agents: [], defaultAgent: null }
  return { useAgents: () => AGENTS }
})
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop: string) => vi.fn().mockResolvedValue(prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}),
  }),
  fileReadUrl: (p: string) => p,
}))

/** The phone form's query reads the live width, as a browser's does. */
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => {
    const max = /max-width:\s*(\d+)px/.exec(q)
    return {
      get matches() { return max ? window.innerWidth <= Number(max[1]) : false },
      media: q, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    }
  }),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as never
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import ChatPage from '../pages/ChatPage'

/** Duo CSS widths: the inner display in landscape and in portrait, the outer
 *  display in portrait. */
const INNER_LANDSCAPE = 951
const INNER_PORTRAIT = 669
const OUTER_PORTRAIT = 382

async function resizeTo(width: number) {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: width })
  await act(async () => { window.dispatchEvent(new Event('resize')) })
}

async function renderChatWithPanelOpen() {
  const store = createTestStore()
  act(() => {
    store.dispatch(sseConnected())
    store.dispatch(sseSlots([{ key: 'slot-a', title: 'Session A' }] as never))
    store.dispatch(setActiveSlot('slot-a'))
    store.dispatch(openActivityPanel())
  })
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  await act(async () => { render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat?sid=slot-a']}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage />} />
            </Routes>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  ) })
  return store
}

const answerBox = () => screen.getByLabelText<HTMLTextAreaElement>('Answer')
const panelColumn = () => document.querySelector('#activity-bar-slot [data-testid="side-panel-column"]')
const phoneOverlay = () => document.querySelector('[data-testid="side-panel-inline"]')

describe('ChatPage: the side panel across a fold', () => {
  beforeEach(() => {
    panel.mounts = 0
    panel.unmounts = 0
    panel.onPageAtMount.length = 0
    pendingSettles.length = 0
    localStorage.clear()
    // The shell's activity column the desktop form portals the panel into.
    const column = document.createElement('div')
    column.id = 'activity-bar-slot'
    document.body.appendChild(column)
    Object.defineProperty(window, 'innerHeight', { writable: true, configurable: true, value: 844 })
  })
  afterEach(() => {
    cleanup()
    document.getElementById('activity-bar-slot')?.remove()
  })

  it('keeps a typed draft through a fold and an unfold (951 -> 382 -> 951)', async () => {
    await resizeTo(INNER_LANDSCAPE)
    await renderChatWithPanelOpen()
    expect(panelColumn()).not.toBeNull()
    fireEvent.change(answerBox(), { target: { value: 'ship it after the canary' } })

    await resizeTo(OUTER_PORTRAIT)
    expect(phoneOverlay()).not.toBeNull()
    expect(panelColumn()).toBeNull()
    expect(answerBox().value).toBe('ship it after the canary')

    await resizeTo(INNER_LANDSCAPE)
    expect(panelColumn()).not.toBeNull()
    expect(answerBox().value).toBe('ship it after the canary')
    expect(panel.mounts).toBe(1)
  })

  it('keeps a typed draft through an unfold and a fold (382 -> 951 -> 382)', async () => {
    await resizeTo(OUTER_PORTRAIT)
    await renderChatWithPanelOpen()
    expect(phoneOverlay()).not.toBeNull()
    fireEvent.change(answerBox(), { target: { value: 'use the staging account' } })

    await resizeTo(INNER_LANDSCAPE)
    expect(panelColumn()).not.toBeNull()
    expect(phoneOverlay()).toBeNull()
    expect(answerBox().value).toBe('use the staging account')

    await resizeTo(OUTER_PORTRAIT)
    expect(phoneOverlay()).not.toBeNull()
    expect(answerBox().value).toBe('use the staging account')
    expect(panel.mounts).toBe(1)
  })

  it('keeps a typed draft between the two phone-form widths (669 -> 382 -> 669)', async () => {
    await resizeTo(INNER_PORTRAIT)
    await renderChatWithPanelOpen()
    expect(phoneOverlay()).not.toBeNull()
    fireEvent.change(answerBox(), { target: { value: 'rename it to cleanup' } })

    await resizeTo(OUTER_PORTRAIT)
    expect(answerBox().value).toBe('rename it to cleanup')
    await resizeTo(INNER_PORTRAIT)
    expect(answerBox().value).toBe('rename it to cleanup')
    expect(panel.mounts).toBe(1)
  })

  it('hands the health check and the trackpad slide the panel in the column it moved to', async () => {
    await resizeTo(OUTER_PORTRAIT)
    await renderChatWithPanelOpen()
    await resizeTo(INNER_LANDSCAPE)
    expect(sidePanelIn(panelColumn())).toBe(answerBox().parentElement)
  })

  it('still unmounts the panel once it is closed, on either form', async () => {
    await resizeTo(OUTER_PORTRAIT)
    const store = await renderChatWithPanelOpen()
    await act(async () => { store.dispatch(closeActivityPanel()) })
    act(() => { pendingSettles.splice(0).forEach(fn => fn()) })
    expect(phoneOverlay()).toBeNull()
    expect(panel.unmounts).toBe(1)

    await resizeTo(INNER_LANDSCAPE)
    await act(async () => { store.dispatch(openActivityPanel()) })
    expect(panelColumn()).not.toBeNull()
    act(() => { store.dispatch(closeActivityPanel()) })
    // The column animates out with the panel still in it, then both go.
    expect(sidePanelIn(panelColumn())).not.toBeNull()
    await waitFor(() => expect(panelColumn()).toBeNull())
    expect(panel.unmounts).toBe(2)
  })

  it('mounts the panel on the page, so its first effects can scroll and focus', async () => {
    // Open from the page's first render on the phone form, then opened again
    // on each form.
    await resizeTo(OUTER_PORTRAIT)
    const store = await renderChatWithPanelOpen()
    const closeOnPhone = async () => {
      await act(async () => { store.dispatch(closeActivityPanel()) })
      act(() => { pendingSettles.splice(0).forEach(fn => fn()) })
    }
    await closeOnPhone()
    await act(async () => { store.dispatch(openActivityPanel()) })
    expect(phoneOverlay()).not.toBeNull()
    await closeOnPhone()

    await resizeTo(INNER_LANDSCAPE)
    await act(async () => { store.dispatch(openActivityPanel()) })
    expect(panelColumn()).not.toBeNull()
    expect(panel.mounts).toBe(3)
    expect(panel.onPageAtMount).toEqual([true, true, true])
  })

  it('puts the panel scroll offset and focus back after a fold and an unfold', async () => {
    await resizeTo(INNER_LANDSCAPE)
    await renderChatWithPanelOpen()
    const scroller = screen.getByTestId('panel-scroller')
    act(() => {
      scroller.scrollTop = 400
      answerBox().focus()
    })

    await resizeTo(OUTER_PORTRAIT)
    expect(phoneOverlay()?.contains(scroller)).toBe(true)
    expect(scroller.scrollTop).toBe(400)
    expect(document.activeElement).toBe(answerBox())

    await resizeTo(INNER_LANDSCAPE)
    expect(panelColumn()?.contains(scroller)).toBe(true)
    expect(scroller.scrollTop).toBe(400)
    expect(document.activeElement).toBe(answerBox())
  })

  it('still dismisses the phone overlay with a swipe that starts on the panel', async () => {
    await resizeTo(OUTER_PORTRAIT)
    await renderChatWithPanelOpen()
    act(() => { pendingSettles.splice(0).forEach(fn => fn()) })
    const panelRoot = screen.getByTestId('side-panel-root')
    expect(phoneOverlay()?.contains(panelRoot)).toBe(true)
    // A rightward drag over half the screen, the way a finger closes it.
    const swipe = (type: string, clientX: number, timeStamp: number) => {
      const t = { clientX, clientY: 300 } as Touch
      const init: TouchEventInit = { bubbles: true }
      if (type === 'touchend') init.changedTouches = [t]
      else init.touches = [t]
      const e = new TouchEvent(type, init)
      Object.defineProperty(e, 'timeStamp', { value: timeStamp })
      act(() => { panelRoot.dispatchEvent(e) })
    }
    swipe('touchstart', 100, 0)
    swipe('touchmove', 112, 16)
    swipe('touchmove', 300, 200)
    swipe('touchend', 300, 216)
    act(() => { pendingSettles.splice(0).forEach(fn => fn()) })

    await waitFor(() => expect(phoneOverlay()).toBeNull())
    expect(panel.unmounts).toBe(1)
  })
})
