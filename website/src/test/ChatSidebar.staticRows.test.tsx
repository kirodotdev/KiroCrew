/**
 * The staticRows half of the compositor-drawer pairing.
 *
 * The mobile drawer's slide runs on the compositor (WAAPI), which Framer's
 * layout projection cannot see: a projection node (`layout`/`layoutId`) under a
 * compositor-driven ancestor attributes the panel's travel to ITSELF and
 * compounds a corrective transform per re-measure — measured at >4,000px, seen
 * by the user as the session rows flying in from the panel's right edge.
 *
 * The session rows are the sidebar's only projection nodes, so `staticRows`
 * is the whole containment:
 *   - ON  (the drawer): rows carry NO layout/layoutId;
 *   - OFF (desktop):    rows keep both, because the desktop panel's motion is
 *     framer-owned and the row projection buys reorder/pin glide and the
 *     flat↔tree morph there.
 *
 * Two-sided on purpose — this file failing on the OFF side means desktop lost
 * its row animations; failing on the ON side means the mobile drawer regressed
 * back to the fly-in bug (see animateDrawer.compositor.test.ts for the WAAPI
 * half of the pairing).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import ChatSidebar from '../pages/ChatSidebar'

/** layoutId values framer received, per render pass. The framer mock maps
 *  layoutId onto data-layout-id, so the DOM carries the answer directly. */
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: unknown) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (k === 'layout') { clean['data-layout'] = String(props[k]); continue }
        if (FRAMER.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref } as never, props.children as never)
    })
  return {
    motion: new Proxy({}, { get: (_t, tag: string) => make(tag) }),
    AnimatePresence: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
    LayoutGroup: ({ children }: { children?: unknown }) => React.createElement(React.Fragment, null, children as never),
  }
})
vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, { get: () => vi.fn().mockResolvedValue([]) }),
}))

const SLOTS = [
  { key: 's1', session_id: 'sid1', title: 'One', mode: 'default', agent: 'kirocrew', last_message_at: new Date().toISOString() },
  { key: 's2', session_id: 'sid2', title: 'Two', mode: 'default', agent: 'kirocrew', last_message_at: new Date().toISOString() },
] as never[]

function renderSidebar(staticRows: boolean) {
  const store = createTestStore({ dashboard: { slots: SLOTS, unreadSlots: [], slotsLoaded: true } as never })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}><QueryClientProvider client={qc}><MemoryRouter><ThemeProvider>
      <ChatSidebar
        slots={SLOTS as never}
        activeSlot={'s1'}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent={'kirocrew'}
        installedAgents={[]}
        mode={'default'}
        staticRows={staticRows}
      />
    </ThemeProvider></MemoryRouter></QueryClientProvider></Provider>,
  )
}

const rowNodes = (c: HTMLElement) => Array.from(c.querySelectorAll('[data-slot-key]'))

describe('ChatSidebar staticRows — projection gated off inside the compositor drawer', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockImplementation((q: string) => ({
        matches: false, media: q, onchange: null,
        addListener: vi.fn(), removeListener: vi.fn(),
        addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
      })),
    })
    globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never
  })
  afterEach(() => vi.clearAllMocks())

  it('staticRows strips layout AND layoutId from every session row', () => {
    const { container } = renderSidebar(true)
    const rows = rowNodes(container)
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      expect(row.getAttribute('data-layout-id')).toBeNull()
      // Projection off is spelled `layout={false}` (upstream's disable form);
      // `undefined` is equally inert. Either way it must not be 'position' —
      // the probe showed layoutId is the load-bearing half, pinned above.
      expect(['undefined', 'false']).toContain(row.getAttribute('data-layout'))
    }
  })

  it('desktop keeps the row projection (both halves of the tradeoff hold)', () => {
    const { container } = renderSidebar(false)
    const rows = rowNodes(container)
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      expect(row.getAttribute('data-layout-id')).toMatch(/^slot-/)
      expect(row.getAttribute('data-layout')).toBe('position')
    }
  })
})
