/**
 * ChatSidebar row windowing, end to end through renderSessionRow: with an
 * IntersectionObserver present, rows past the first displacement window render
 * as stubs that keep their place and identity in the lane, the active row stays
 * mounted wherever it sits, and a row the observer brings near mounts for real.
 * The component contract itself is pinned in sessionRowWindow.test.tsx; this
 * file pins the WIRING, which is what a refactor of the 11k-line shell breaks.
 */
import React from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
// Components are cached per tag (same mock as ChatSidebar.rowMemo.test.tsx), so
// a motion ancestor does not remount the rows below it on every render.
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  type MockProps = Record<string, unknown> & { children?: React.ReactNode }
  const make = (tag: string) =>
    React.forwardRef((props: MockProps, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const cache = new Map<string, unknown>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!cache.has(tag)) cache.set(tag, make(tag))
      return cache.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: MockProps) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: MockProps) => React.createElement(React.Fragment, null, children),
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

import ChatSidebar, { SIDEBAR_DISPLACEMENT_WINDOW } from '../pages/ChatSidebar'
import { _resetSessionRowWindowHeights } from '../pages/chat/sessionRowWindow'

type Cb = (entries: Array<Partial<IntersectionObserverEntry>>) => void
let observers: Array<{ cb: Cb; els: Set<Element>; root: Element | null }> = []
class MockIO {
  private rec: { cb: Cb; els: Set<Element>; root: Element | null }
  constructor(cb: Cb, opts?: IntersectionObserverInit) {
    this.rec = { cb, els: new Set(), root: (opts?.root as Element) ?? null }
    observers.push(this.rec)
  }
  observe(el: Element) { this.rec.els.add(el) }
  unobserve(el: Element) { this.rec.els.delete(el) }
  disconnect() { this.rec.els.clear() }
  takeRecords() { return [] }
}

const EMPTY: never[] = []
const N = SIDEBAR_DISPLACEMENT_WINDOW + 30
const slots = Array.from({ length: N }, (_, i) => ({ key: `k-${i}`, title: `Session ${i}`, running: false, modified: 2_000_000_000 - i * 60 }))

function renderSidebar(activeSlot: string | null) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      subagentQueued: {}, goalLoops: {}, workflowRuns: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar slots={slots} activeSlot={activeSlot} unreadSlots={EMPTY}
              history={EMPTY} historyHasMore={false} defaultAgent="" installedAgents={EMPTY} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const rowIds = () => Array.from(document.querySelectorAll('[data-session-row]')).map(e => e.getAttribute('data-session-row'))
const isStub = (key: string) => !!document.querySelector(`[data-session-row="${key}"]`)?.closest('[data-session-window="stub"]')

describe('ChatSidebar row windowing', () => {
  beforeEach(() => {
    observers = []
    _resetSessionRowWindowHeights()
    localStorage.clear()
    vi.stubGlobal('IntersectionObserver', MockIO)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('stubs rows past the displacement window but keeps every row in DOM order', () => {
    renderSidebar(null)
    expect(rowIds()).toEqual(slots.map(s => s.key))
    expect(isStub('k-0')).toBe(false)
    expect(isStub(`k-${SIDEBAR_DISPLACEMENT_WINDOW - 1}`)).toBe(false)
    expect(isStub(`k-${SIDEBAR_DISPLACEMENT_WINDOW}`)).toBe(true)
    expect(isStub(`k-${N - 1}`)).toBe(true)
    // Real rows exist only for the mounted ones.
    expect(document.querySelectorAll('.session-row')).toHaveLength(SIDEBAR_DISPLACEMENT_WINDOW)
  })

  it('keeps stub and real rows on the same DOM identity contract', () => {
    renderSidebar(null)
    const realKey = 'k-0'
    const stubKey = `k-${SIDEBAR_DISPLACEMENT_WINDOW}`
    const real = document.querySelector<HTMLElement>(`.session-row[data-session-row="${realKey}"]`)!
    const stub = document.querySelector<HTMLElement>(`[data-session-window="stub"] [data-session-row="${stubKey}"]`)!
    const identityAttributes = (el: HTMLElement) => Array.from(el.attributes)
      .map(attribute => attribute.name)
      .filter(name => name.startsWith('data-session-'))
      .sort()

    expect(identityAttributes(stub)).toEqual(identityAttributes(real))
    expect(stub.getAttribute('role')).toBe(real.getAttribute('role'))
    expect(stub.getAttribute('tabindex')).toBe(real.getAttribute('tabindex'))
    expect(real.closest('[data-slot-key]')).toHaveAttribute('data-slot-key', realKey)
    expect(stub.closest('[data-slot-key]')).toHaveAttribute('data-slot-key', stubKey)
  })

  it('observes rows against the lane scroller, not the viewport', () => {
    renderSidebar(null)
    const lane = document.querySelector('[data-testid="tree-view-lane"]')
    const o = observers.find(r => r.root === lane)
    expect(o).toBeTruthy()
    expect(o!.els.size).toBe(N)
  })

  it('keeps the active row mounted when it sits past the window', () => {
    renderSidebar(`k-${N - 1}`)
    expect(isStub(`k-${N - 1}`)).toBe(false)
    expect(isStub(`k-${N - 2}`)).toBe(true)
  })

  it('mounts a stubbed row when the observer reports it near', () => {
    renderSidebar(null)
    const key = `k-${N - 5}`
    const slot = document.querySelector(`[data-session-row="${key}"]`)!.closest('[data-session-window]')!
    const o = observers.find(r => r.els.has(slot))!
    act(() => o.cb([{ target: slot, isIntersecting: true, boundingClientRect: { height: 0 } as DOMRectReadOnly }]))
    expect(isStub(key)).toBe(false)
    expect(document.querySelector(`.session-row[data-session-row="${key}"]`)).toBeTruthy()
  })
})
