/**
 * Switch All Sessions panel: a model-roster fetch failure must be visible.
 *
 * The panel's list comes from the shared `['available-models', 'acp']` query,
 * and the ACP adapter never rejects that query: a failed `/api/models` resolves
 * with the last-good cached list or Auto alone and flips the provider's degraded
 * flag. Without a notice, the panel shows that one-entry list as if it were the
 * whole catalog, and the user can apply Auto to every session — which resets
 * every conversation — while believing their model simply went away.
 *
 * Each failure signature the adapter can produce gets its own case, plus the
 * live-success case that must NOT show the notice. Radix DropdownMenu cannot be
 * opened by mouse in jsdom, so the header ⋮ is activated by keyboard.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { markModelsDegraded } from '../providers/modelListHealth'
import type { RootState } from '../store'

// Render framer-motion elements as plain DOM because jsdom cannot run projection.
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

vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false, defaultAutopilot: false }),
  saveChatConfig: vi.fn(),
}))

const mocks = vi.hoisted(() => ({
  models: vi.fn(),
  chatSlotsModel: vi.fn(),
  chatFolders: vi.fn(),
  chatTags: vi.fn(),
  tagColumns: vi.fn(),
  kirocrewConfig: vi.fn(),
  sessions: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as unknown as Record<string, unknown>, {
    get: (target, prop: string) => (prop in target ? target[prop] : vi.fn().mockResolvedValue([])),
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

const SLOTS = [
  { key: 'k-a', title: 'Idle A', running: false, messages: 1 },
  { key: 'k-b', title: 'Idle B', running: false, messages: 1 },
]

const LIVE_ROSTER = [
  { model_name: 'auto', description: 'Default' },
  { model_name: 'opus-4.8', description: 'Opus' },
  { model_name: 'sonnet-4.7', description: 'Sonnet' },
]

/** The adapter's own cache slot: a good list from an earlier live fetch. */
const MODELS_CACHE_KEY = 'kc.acp.models.v1'

function renderSidebar() {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots: SLOTS, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {},
      automations: {}, workflowRuns: {}, subagentQueued: {}, slotHistory: [],
      revealRequest: null, revealNonce: 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['tag-columns'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={SLOTS} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false}
              defaultAgent="" installedAgents={[{ name: 'builder', source: 'builtin' }]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

async function openSwitchAllPanel() {
  fireEvent.keyDown(screen.getAllByLabelText('More options')[0], { key: 'Enter' })
  fireEvent.click(await screen.findByText('Switch all to model…'))
  expect(screen.getByText('Switch All Sessions')).toBeTruthy()
}

/** The model IDS the listbox offers, in order — read off `data-model-id`, the
 *  value a pick sends, not the row text (Auto's row carries a translated
 *  description and a live roster may carry a credit badge). */
const optionIds = () =>
  within(screen.getByRole('listbox', { name: 'Model list' }))
    .getAllByRole('option')
    .map(o => o.querySelector('[data-model-id]')?.getAttribute('data-model-id'))

beforeEach(() => {
  localStorage.clear()
  // The degraded flag is module-level and survives between tests; every case
  // starts from "never fetched" so the notice it observes is its own fetch's.
  markModelsDegraded('acp', false)
  mocks.chatSlotsModel.mockResolvedValue({ ok: true, failed: [] })
  mocks.chatFolders.mockResolvedValue([])
  mocks.chatTags.mockResolvedValue([])
  mocks.tagColumns.mockResolvedValue([])
  mocks.sessions.mockResolvedValue({ sessions: [], has_more: false })
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: { recent_tint_count: 3 } })
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('ChatSidebar — Switch All Sessions roster failure', () => {
  it('shows the notice when /api/models rejects and only Auto is offered', async () => {
    mocks.models.mockRejectedValue(new Error('503'))
    renderSidebar()
    await openSwitchAllPanel()
    const notice = await screen.findByTestId('bulk-model-roster-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain("Couldn't load the model list")
    // The placeholder list is still rendered underneath: the notice explains
    // it, it does not replace it.
    expect(optionIds()).toEqual(['auto'])
  })

  it('shows the notice when /api/models resolves with an empty list', async () => {
    mocks.models.mockResolvedValue([])
    renderSidebar()
    await openSwitchAllPanel()
    expect(await screen.findByTestId('bulk-model-roster-error')).toBeTruthy()
    expect(optionIds()).toEqual(['auto'])
  })

  it('shows the notice when a failed fetch serves the last-good cached list', async () => {
    // The hardest case to notice by eye: the list is populated, just stale.
    localStorage.setItem(MODELS_CACHE_KEY, JSON.stringify({
      ts: Date.now(),
      models: [{ name: 'auto', description: '' }, { name: 'opus-4.8', description: 'Opus' }],
    }))
    mocks.models.mockRejectedValue(new Error('network'))
    renderSidebar()
    await openSwitchAllPanel()
    expect(await screen.findByTestId('bulk-model-roster-error')).toBeTruthy()
    expect(optionIds()).toEqual(['auto', 'opus-4.8'])
  })

  it('shows nothing on a live success', async () => {
    mocks.models.mockResolvedValue(LIVE_ROSTER)
    renderSidebar()
    await openSwitchAllPanel()
    await waitFor(() => expect(optionIds()).toEqual(['auto', 'opus-4.8', 'sonnet-4.7']))
    expect(screen.queryByTestId('bulk-model-roster-error')).toBeNull()
  })

  it('Retry refetches the roster and the notice clears on a live success', async () => {
    mocks.models.mockRejectedValueOnce(new Error('503')).mockResolvedValue(LIVE_ROSTER)
    renderSidebar()
    await openSwitchAllPanel()
    await screen.findByTestId('bulk-model-roster-error')
    expect(mocks.models).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mocks.models).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('bulk-model-roster-error')).toBeNull())
    expect(optionIds()).toEqual(['auto', 'opus-4.8', 'sonnet-4.7'])
  })

  it('unpicks a cached model the refreshed roster no longer lists', async () => {
    // The degraded list is the last-good cache, so a model can be picked from
    // it that a successful Retry then drops. The backend accepts any
    // non-registry id, so a Switch with the stale pick would reset every
    // session onto a model kiro-cli refuses. The pick must not survive the
    // roster that dropped it.
    localStorage.setItem(MODELS_CACHE_KEY, JSON.stringify({
      ts: Date.now(),
      models: [{ name: 'auto', description: '' }, { name: 'opus-4.8', description: 'Opus' }, { name: 'retired-1', description: 'Gone' }],
    }))
    mocks.models.mockRejectedValueOnce(new Error('503')).mockResolvedValue(LIVE_ROSTER)
    renderSidebar()
    await openSwitchAllPanel()
    await screen.findByTestId('bulk-model-roster-error')
    await waitFor(() => expect(optionIds()).toEqual(['auto', 'opus-4.8', 'retired-1']))
    fireEvent.click(screen.getByRole('option', { name: /retired-1/ }))
    const switchBtn = () => screen.getByRole('button', { name: /^Switch \d+ sessions?$/ })
    expect(switchBtn()).not.toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(optionIds()).toEqual(['auto', 'opus-4.8', 'sonnet-4.7']))
    // No option is selected any more, and Switch is inert until a new pick.
    expect(screen.queryByRole('option', { selected: true })).toBeNull()
    expect(switchBtn()).toBeDisabled()
    fireEvent.click(switchBtn())
    expect(mocks.chatSlotsModel).not.toHaveBeenCalled()
    // A pick that IS listed still arms Switch as before.
    fireEvent.click(screen.getByRole('option', { name: /sonnet-4\.7/ }))
    expect(switchBtn()).not.toBeDisabled()
    fireEvent.click(switchBtn())
    await waitFor(() => expect(mocks.chatSlotsModel).toHaveBeenCalledWith('sonnet-4.7', true, undefined))
  })

  it('keeps the notice out of the Cancel/Switch row and above the listbox', async () => {
    // The button row is at the two-button limit and an inline notice inside it
    // wraps one character per line at sidebar width (#10814's finding), so the
    // roster notice owns a row of its own, before the list it explains.
    mocks.models.mockRejectedValue(new Error('503'))
    renderSidebar()
    await openSwitchAllPanel()
    const notice = await screen.findByTestId('bulk-model-roster-error')
    const row = screen.getByText('Cancel').closest('button')!.parentElement!
    expect(row.contains(notice)).toBe(false)
    const listbox = screen.getByRole('listbox', { name: 'Model list' })
    expect(listbox.compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
    // Block variant: the boxed banner, not the inline-flex span.
    expect(notice.tagName).toBe('DIV')
    expect(notice.className).not.toMatch(/\binline-flex\b/)
  })
})
