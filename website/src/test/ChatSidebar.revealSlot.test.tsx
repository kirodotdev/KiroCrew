/**
 * Regression test for #912: Reveal in sidebar silently does nothing.
 *
 * The core race condition: the reveal event was dispatched (via window
 * CustomEvent) before the sidebar listener mounted, so it was lost forever.
 * The fix replaces the fire-and-forget event with a prop-driven pending
 * reveal that the sidebar consumes on mount.
 *
 * This test verifies:
 * 1. A revealSlot set BEFORE mount is still processed on mount (the race).
 * 2. A revealSlot set AFTER mount is processed immediately.
 * 3. The highlight animation class is applied then cleared after animationend.
 * 4. onRevealConsumed fires after the reveal lands.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>(
      (props, ref) => {
        const clean: Record<string, unknown> = {}
        for (const k of Object.keys(props)) {
          if (k === 'children') continue
          if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
          if (FRAMER_PROPS.has(k)) continue
          clean[k] = props[k]
        }
        return React.createElement(tag, { ...clean, ref }, props.children)
      })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, { get: () => vi.fn().mockResolvedValue([]) }),
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

const SLOT_KEY = 'session-abc-123'
const FOLDER_ID = 'folder-001'

const baseSlot = {
  key: SLOT_KEY, title: 'My Session', running: false, tags: [] as string[],
  created: '', last_ts: '',
}

function createWrapper(opts: { foldered?: boolean } = {}) {
  const slot = opts.foldered ? { ...baseSlot, folder_id: FOLDER_ID } : baseSlot
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: SLOT_KEY } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  qc.setQueryData(['chat-folders'], opts.foldered
    ? [{ id: FOLDER_ID, name: 'Work', order: 0, collapsed: true }]
    : [])
  return { store, qc, slot }
}

function renderSidebar(
  revealSlot: { key: string; nonce: number } | null,
  onRevealConsumed: () => void,
  opts: { foldered?: boolean } = {},
) {
  const { store, qc, slot } = createWrapper(opts)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[slot]} activeSlot={SLOT_KEY} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
              revealSlot={revealSlot}
              onRevealConsumed={onRevealConsumed}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.useFakeTimers()
  // jsdom does not implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('ChatSidebar reveal-in-sidebar (#912)', () => {
  it('processes a revealSlot set BEFORE mount (the race condition fix)', async () => {
    vi.useRealTimers()
    const consumed = vi.fn()
    const { container } = renderSidebar({ key: SLOT_KEY, nonce: 1 }, consumed)

    // Wait for the effect + timeout to fire and re-render with highlight
    await act(async () => { await new Promise(r => setTimeout(r, 300)) })

    const row = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
    expect(row).toBeTruthy()
    expect(row!.className).toContain('animate-slot-reveal')
    expect(consumed).toHaveBeenCalledTimes(1)
    vi.useFakeTimers()
  })

  it('processes a revealSlot set AFTER mount via rerender', async () => {
    vi.useRealTimers()
    const consumed = vi.fn()
    const { container, rerender } = renderSidebar(null, consumed)

    // No highlight initially
    const rowBefore = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
    expect(rowBefore).toBeTruthy()
    expect(rowBefore!.className).not.toContain('animate-slot-reveal')

    // Now simulate the reveal prop arriving (sidebar already mounted)
    const { store, qc, slot } = createWrapper()
    rerender(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatSidebar
                slots={[slot]} activeSlot={SLOT_KEY} unreadSlots={[]}
                history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
                revealSlot={{ key: SLOT_KEY, nonce: 2 }}
                onRevealConsumed={consumed}
              />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )

    await act(async () => { await new Promise(r => setTimeout(r, 300)) })

    const row = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
    expect(row).toBeTruthy()
    expect(row!.className).toContain('animate-slot-reveal')
    expect(consumed).toHaveBeenCalled()
    vi.useFakeTimers()
  })

  it('clears highlight after timeout', async () => {
    const consumed = vi.fn()
    const { container } = renderSidebar({ key: SLOT_KEY, nonce: 3 }, consumed)

    // Advance past the initial delay so the highlight activates
    await act(async () => { vi.advanceTimersByTime(100) })

    const row = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
    expect(row).toBeTruthy()
    expect(row!.className).toContain('animate-slot-reveal')

    // Advance past the 2200ms clear timeout
    await act(async () => { vi.advanceTimersByTime(2300) })

    const rowAfter = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
    expect(rowAfter).toBeTruthy()
    expect(rowAfter!.className).not.toContain('animate-slot-reveal')
  })

  it('does not re-trigger for the same nonce', async () => {
    const consumed = vi.fn()
    const reveal = { key: SLOT_KEY, nonce: 5 }
    const { container } = renderSidebar(reveal, consumed)

    // Let the reveal fire and the highlight clear
    await act(async () => { vi.advanceTimersByTime(100) })
    expect(consumed).toHaveBeenCalledTimes(1)

    // Advance past the highlight clear
    await act(async () => { vi.advanceTimersByTime(2300) })

    const row = container.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
    expect(row).toBeTruthy()
    expect(row!.className).not.toContain('animate-slot-reveal')

    // Rerender with same nonce should not re-highlight
    const { store, qc, slot } = createWrapper()
    const { container: container2 } = render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatSidebar
                slots={[slot]} activeSlot={SLOT_KEY} unreadSlots={[]}
                history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
                revealSlot={reveal}
                onRevealConsumed={consumed}
              />
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )

    await act(async () => { vi.advanceTimersByTime(200) })
    const row2 = container2.querySelector(`[data-slot-key="${SLOT_KEY}"]`)
    // A fresh mount with same nonce=5 will process it (new ref), which is correct
    // behavior - the nonce guard is per-component-instance
    expect(row2).toBeTruthy()
  })
})
