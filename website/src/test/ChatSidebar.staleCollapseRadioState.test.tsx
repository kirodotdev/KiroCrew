/**
 * The sort-and-filter menu's dormant-collapse threshold presets are a third
 * mutually exclusive group beside the two "Sort sessions by" / "Folder order"
 * groups. On a phone they render inline as duration pills; before this fix the
 * pills were toggle buttons carrying `aria-pressed`, which announces an on/off
 * toggle rather than "which threshold is in effect" — so a screen reader could
 * not tell the chosen threshold from the rest of a single-select set.
 *
 * Each pill is now a `role="radio"` carrying `aria-checked`, wrapped in a
 * `role="radiogroup"` named by the section caption: exactly the selected
 * threshold is `aria-checked="true"`, every sibling `"false"`. This is the same
 * grammar the desktop submenu uses (`menuitemradio`), applied to the inline form
 * so the two surfaces agree.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
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
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const serverConfig: { dashboard: Record<string, unknown> } = { dashboard: {} }
const patchConfig = vi.fn(async () => ({ ok: true }))
const kirocrewConfig = vi.fn(async () => ({ dashboard: { ...serverConfig.dashboard } }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, name: string) => {
      if (name === 'patchConfig') return patchConfig
      if (name === 'kirocrewConfig') return kirocrewConfig
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

// A phone-width viewport: `useIsMobile()` matches, so the dormant-collapse
// thresholds render inline as duration pills rather than inside a flyout submenu
// (a flyout cannot fit beside a phone-width menu). The inline form is the one
// that used `aria-pressed`, so it is the surface under test here.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: /max-width/.test(q),
    media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import type { RootState } from '../store'
import type { ChatFolder, ChatSlot } from '../types'
import { STALE_COLLAPSE_PRESETS_MS } from '../pages/staleCollapse'
import { STALE_COLLAPSE_LS_KEY } from '../pages/chat-sidebar/persistence'

const FOLDERS: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0, collapsed: true, created_at: 1_000 },
]

function renderSidebar(staleCollapseMs: number) {
  localStorage.setItem(STALE_COLLAPSE_LS_KEY, String(staleCollapseMs))
  const slots: ChatSlot[] = []
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], FOLDERS)
  serverConfig.dashboard = {}
  qc.setQueryData(['kirocrewConfig'], { dashboard: {} })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The radiogroup the dormant-collapse presets live in, and its radios. */
async function openDormantPresets(findByTestId: (id: string) => Promise<HTMLElement>, getByLabelText: (t: string) => HTMLElement) {
  fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
  await findByTestId('stale-collapse-menu')
  const group = document.querySelector('[role="radiogroup"][aria-labelledby="stale-collapse-presets-label"]') as HTMLElement
  const radios = Array.from(group.querySelectorAll('[role="radio"]')) as HTMLElement[]
  return { group, radios }
}

beforeEach(() => { localStorage.clear(); patchConfig.mockClear(); kirocrewConfig.mockClear() })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — dormant-collapse presets announce the checked threshold (#21486)', () => {
  it('renders every threshold pill as a radio in a named radiogroup, checked only on the active threshold', async () => {
    const active = 2 * 24 * 60 * 60 * 1000 // 2 days — a preset other than the default
    const { getByLabelText, findByTestId } = renderSidebar(active)
    const { group, radios } = await openDormantPresets(findByTestId, getByLabelText)

    // One radio per preset threshold, the whole set under one radiogroup.
    expect(radios.length).toBe(STALE_COLLAPSE_PRESETS_MS.length)
    expect(group.getAttribute('aria-labelledby')).toBe('stale-collapse-presets-label')

    // Exactly one radio is checked, and it is the active threshold.
    const checked = radios.filter(r => r.getAttribute('aria-checked') === 'true')
    expect(checked.length).toBe(1)
    // "2d" is the visible label the shared duration formatter produces for the
    // active threshold; the others (Off, 1d, 7d, 14d) announce unchecked.
    expect((checked[0].textContent ?? '')).toContain('2d')
  })

  it('carries state on aria-checked, never aria-pressed (a single-select group, not a toggle)', async () => {
    const { getByLabelText, findByTestId } = renderSidebar(24 * 60 * 60 * 1000)
    const { radios } = await openDormantPresets(findByTestId, getByLabelText)

    for (const r of radios) {
      expect(r.getAttribute('role')).toBe('radio')
      expect(r.hasAttribute('aria-pressed')).toBe(false)
      expect(['true', 'false']).toContain(r.getAttribute('aria-checked'))
    }
  })

  it('picking a threshold pill writes the chosen ms and re-announces the checked row', async () => {
    const { getByLabelText, findByTestId } = renderSidebar(0) // start "Off"
    const { radios } = await openDormantPresets(findByTestId, getByLabelText)

    const off = radios.find(r => r.getAttribute('aria-checked') === 'true')!
    expect((off.textContent ?? '')).toMatch(/off/i)

    const oneDay = radios.find(r => (r.textContent ?? '').trim() === '1d')!
    fireEvent.click(oneDay)

    await waitFor(() => {
      expect(oneDay.getAttribute('aria-checked')).toBe('true')
      expect(off.getAttribute('aria-checked')).toBe('false')
    })
    expect(localStorage.getItem(STALE_COLLAPSE_LS_KEY)).toBe(String(24 * 60 * 60 * 1000))
  })
})

// A radio is operated as a GROUP (WAI-ARIA radio pattern): one Tab stop, arrow
// keys move focus and selection together. `role="radio"` without this promised
// screen-reader and keyboard users a behaviour the pills did not have. These use
// real key events through user-event, so the host Radix menu's own keydown
// handling runs too — it must not steal the arrow keys from the group.
describe('chat sidebar — dormant-collapse presets are operable as a radio group (#21486)', () => {
  const DAY = 24 * 60 * 60 * 1000
  const checkedLabel = (radios: HTMLElement[]) =>
    (radios.find(r => r.getAttribute('aria-checked') === 'true')?.textContent ?? '').trim()

  it('makes only the checked pill a Tab stop (roving tabindex)', async () => {
    const { getByLabelText, findByTestId } = renderSidebar(2 * DAY)
    const { radios } = await openDormantPresets(findByTestId, getByLabelText)
    const tabbable = radios.filter(r => r.tabIndex === 0)
    expect(tabbable.length).toBe(1)
    expect(tabbable[0].getAttribute('aria-checked')).toBe('true')
  })

  it('ArrowRight / ArrowLeft move focus AND selection to the adjacent pill, wrapping at the ends', async () => {
    const user = userEvent.setup()
    const { getByLabelText, findByTestId } = renderSidebar(DAY) // "1d", second preset
    const { radios } = await openDormantPresets(findByTestId, getByLabelText)
    const oneDay = radios.find(r => (r.textContent ?? '').trim() === '1d')!
    act(() => oneDay.focus())

    await user.keyboard('{ArrowRight}')
    await waitFor(() => expect(checkedLabel(radios)).toBe('2d'))
    expect(document.activeElement?.textContent?.trim()).toBe('2d')
    expect(localStorage.getItem(STALE_COLLAPSE_LS_KEY)).toBe(String(2 * DAY))

    await user.keyboard('{ArrowLeft}{ArrowLeft}') // 2d -> 1d -> Off
    await waitFor(() => expect(checkedLabel(radios)).toMatch(/off/i))
    await user.keyboard('{ArrowLeft}') // wraps from the first to the last
    await waitFor(() => expect(checkedLabel(radios)).toBe('14d'))
    expect(document.activeElement).toBe(radios[radios.length - 1])
    // The single Tab stop follows the selection.
    expect(radios.filter(r => r.tabIndex === 0)).toEqual([radios[radios.length - 1]])
  })

  it('Home / End jump to the first / last pill', async () => {
    const user = userEvent.setup()
    const { getByLabelText, findByTestId } = renderSidebar(2 * DAY)
    const { radios } = await openDormantPresets(findByTestId, getByLabelText)
    act(() => radios.find(r => r.getAttribute('aria-checked') === 'true')!.focus())

    await user.keyboard('{End}')
    await waitFor(() => expect(checkedLabel(radios)).toBe('14d'))
    await user.keyboard('{Home}')
    await waitFor(() => expect(checkedLabel(radios)).toMatch(/off/i))
    expect(document.activeElement).toBe(radios[0])
  })
})
