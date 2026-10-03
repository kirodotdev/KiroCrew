/**
 * Filter-menu Models section: narrows the session list to the models the
 * selected sessions run on, or to everything EXCEPT them.
 *
 * The vocabulary is the slot list itself, not the registry: a row exists only
 * for a model some session uses, so the section also reads as "what runs where".
 * Same harness as the Tags section test, since the two are sibling dimensions.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, renderHook, fireEvent, waitFor } from '@testing-library/react'
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
import { DEFAULT_MODEL_FILTER_KEY, slotModelKey, useSidebarModels } from '../pages/chat-sidebar/models'
import type { Slot } from '../pages/chat-sidebar/types'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const STORAGE_KEY = 'mc-session-model-filter'

/** Four sessions on three models. Two spellings of Fable (registry alias and
 *  provider id) fold onto one row labelled with the smallest spelling; the
 *  unpinned one groups under the No-model-chosen row. */
const SLOTS = [
  { key: 'k-fable-a', title: 'fable alpha', running: false, messages: 2, model: 'claude-fable-5' },
  { key: 'k-fable-b', title: 'fable beta', running: false, messages: 2, model: 'global.anthropic.claude-fable-5[1m]' },
  { key: 'k-opus', title: 'opus session', running: false, messages: 2, model: 'claude-opus-5.5' },
  { key: 'k-default', title: 'default session', running: false, messages: 2 },
]
const ALL_TITLES = SLOTS.map(s => s.title)

function renderSidebar(slots: ChatSlot[] = SLOTS as ChatSlot[], revealRequest: { kind: 'session' | 'folder'; target: string; nonce: number } | null = null) {
  const defaults = createTestStore().getState()
  const store = createTestStore({
    dashboard: {
      ...defaults.dashboard,
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      ...defaults.chat,
      activeSlot: null, slotStatusDetail: {},
      revealRequest, revealNonce: revealRequest?.nonce ?? 0,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['chat-tags'], [])
  const view = render(
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
  return { ...view, store }
}

function openFilterMenu(utils: ReturnType<typeof renderSidebar>) {
  fireEvent.keyDown(utils.getByLabelText('Sort and filter sessions'), { key: 'Enter' })
}

/** Which of the four titles are on screen right now. */
function visible(utils: ReturnType<typeof renderSidebar>) {
  return ALL_TITLES.filter(title => utils.queryByText(title) !== null)
}

function stored() {
  return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{"keys":[],"exclude":false}')
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — filter menu Models section', () => {
  it('lists only the models in use, most used first, folding spelling variants onto one row', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).not.toBeNull())
    openFilterMenu(utils)
    const fable = await utils.findByTestId('model-filter-row-fable-5-1m')
    expect(fable).toHaveAttribute('role', 'menuitemcheckbox')
    expect(fable).toHaveAttribute('aria-checked', 'false')
    // Registry display name, and both spellings counted together.
    expect(fable).toHaveTextContent('claude-fable-5')
    expect(fable).toHaveTextContent('2')
    // An id the registry does not know keeps its id as the label.
    expect(await utils.findByTestId('model-filter-row-claude-opus-5-5')).toHaveTextContent('claude-opus-5.5')
    // Sessions with no pin group under one Default row.
    expect(await utils.findByTestId('model-filter-row-auto')).toHaveTextContent('No model chosen (agent default)')
    // Most-used first: the section doubles as a read of what the fleet runs on.
    const rows = utils.getAllByTestId(/^model-filter-row-/)
    expect(rows[0]).toBe(fable)
    // A registry model no session uses gets no row.
    expect(utils.queryByTestId('model-filter-row-opus-4.8-1m')).toBeNull()
    // The invert row is listed from the start so it can be found, but disabled
    // until something is selected: on it would change nothing.
    expect(utils.getByTestId('model-filter-exclude')).toHaveAttribute('aria-disabled', 'true')
  })

  it('narrows the list to the selected model', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('opus session')).not.toBeNull())
    openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('model-filter-row-fable-5-1m'))
    await waitFor(() => expect(visible(utils)).toEqual(['fable alpha', 'fable beta']))
    expect(stored()).toEqual({ keys: ['fable-5-1m'], exclude: false })
  })

  it('treats a multi-model selection as a union', async () => {
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).not.toBeNull())
    openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('model-filter-row-fable-5-1m'))
    fireEvent.click(await utils.findByTestId('model-filter-row-auto'))
    await waitFor(() => expect(visible(utils)).toEqual(['fable alpha', 'fable beta', 'default session']))
  })

  it('shows everything except the selected models once the invert row is on', async () => {
    // The use case the feature exists for: every session NOT on the model I
    // keep, so the stragglers on an older model can be moved.
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).not.toBeNull())
    openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('model-filter-row-fable-5-1m'))
    const invert = await utils.findByTestId('model-filter-exclude')
    expect(invert).not.toHaveAttribute('aria-disabled')
    expect(invert).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(invert)
    await waitFor(() => expect(visible(utils)).toEqual(['opus session', 'default session']))
    expect(invert).toHaveAttribute('aria-checked', 'true')
    expect(stored()).toEqual({ keys: ['fable-5-1m'], exclude: true })
    // The ticked row now reads as hidden: its label is marked excluded.
    expect(utils.getByTestId('model-filter-row-fable-5-1m').querySelector('[data-excluded="true"]')).not.toBeNull()
    expect(utils.getByTestId('model-filter-row-claude-opus-5-5').querySelector('[data-excluded="true"]')).toBeNull()
    // Deselecting the last model drops the direction with it: a lone invert
    // flag would otherwise come back and surprise the next selection.
    fireEvent.click(await utils.findByTestId('model-filter-row-fable-5-1m'))
    await waitFor(() => expect(visible(utils)).toEqual(ALL_TITLES))
    expect(stored()).toEqual({ keys: [], exclude: false })
  })

  it('persists the selection and its direction so both survive a remount', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ keys: ['claude-opus-5-5'], exclude: true }))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).not.toBeNull())
    await waitFor(() => expect(utils.queryByText('opus session')).toBeNull())
    expect(visible(utils)).toEqual(['fable alpha', 'fable beta', 'default session'])
  })

  it('ignores a persisted model no session uses any more instead of hiding everything', async () => {
    // The last session on that model was moved off it. In include mode the key
    // would match nothing and blank the list with no row left to explain why.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ keys: ['sonnet-4.6-1m'], exclude: false }))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).not.toBeNull())
    expect(visible(utils)).toEqual(ALL_TITLES)
    expect(utils.queryByTestId('model-filter-chip')).toBeNull()
  })

  it('does not let a stale key keep the inverted direction alive for the next tick', async () => {
    // Stored "Not <old model>" whose last session is gone. Nothing is ticked on
    // screen, so the invert row must be disabled and unticked, and the first tick must mean
    // "show only X", not "hide X": the stale key is dropped and the direction
    // with it, and the stored shape says so.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ keys: ['sonnet-4.6-1m'], exclude: true }))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).not.toBeNull())
    expect(visible(utils)).toEqual(ALL_TITLES)
    openFilterMenu(utils)
    const invert = utils.getByTestId('model-filter-exclude')
    expect(invert).toHaveAttribute('aria-disabled', 'true')
    // ...and it must not read as ON either: a ticked invert row over an empty
    // selection would announce "hide" while the next tick means "show only".
    expect(invert).toHaveAttribute('aria-checked', 'false')
    expect(utils.getByTestId('model-filter-row-fable-5-1m').getAttribute('title')).toBe('Show only sessions using claude-fable-5')
    fireEvent.click(await utils.findByTestId('model-filter-row-fable-5-1m'))
    await waitFor(() => expect(visible(utils)).toEqual(['fable alpha', 'fable beta']))
    expect(stored()).toEqual({ keys: ['fable-5-1m'], exclude: false })
  })

  it('reads a stored direction with no keys as off', async () => {
    // Not a shape this code writes; a hand-edited one must not invert the next
    // selection from a reload.
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ keys: [], exclude: true }))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).not.toBeNull())
    openFilterMenu(utils)
    fireEvent.click(await utils.findByTestId('model-filter-row-fable-5-1m'))
    await waitFor(() => expect(visible(utils)).toEqual(['fable alpha', 'fable beta']))
  })

  it('names what a tick does once the selection is inverted', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ keys: ['fable-5-1m'], exclude: true }))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).toBeNull())
    openFilterMenu(utils)
    expect(await utils.findByTestId('model-filter-row-claude-opus-5-5')).toHaveAttribute('title', 'Hide sessions using claude-opus-5.5')
    expect(await utils.findByTestId('model-filter-row-fable-5-1m')).toHaveAttribute('title', 'Stop filtering by claude-fable-5')
  })

  it('shows one aggregate chip that names the selection and clears the whole filter', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ keys: ['fable-5-1m', 'auto'], exclude: false }))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('opus session')).toBeNull())
    const chip = await utils.findByTestId('model-filter-chip')
    expect(chip).toHaveTextContent('claude-fable-5 or No model chosen (agent default)')
    expect(chip).toHaveAccessibleName('Clear claude-fable-5 or No model chosen (agent default) filter')
    // One control however many models are selected (AUTOSDE max-two-buttons-per-row).
    expect(chip.parentElement!.querySelectorAll('button')).toHaveLength(1)
    fireEvent.click(chip)
    await waitFor(() => expect(visible(utils)).toEqual(ALL_TITLES))
    expect(stored()).toEqual({ keys: [], exclude: false })
  })

  it('labels the chip with the negation when the selection is inverted', async () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ keys: ['fable-5-1m'], exclude: true }))
    const utils = renderSidebar()
    await waitFor(() => expect(utils.queryByText('fable alpha')).toBeNull())
    const chip = await utils.findByTestId('model-filter-chip')
    expect(chip).toHaveTextContent('Not claude-fable-5')
    expect(chip).toHaveAccessibleName('Clear Not claude-fable-5 filter')
  })

  it('clears the model filter when a reveal target is hidden by it', async () => {
    const scrollIntoView = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scrollIntoView
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ keys: ['fable-5-1m'], exclude: false }))
      const utils = renderSidebar()
      await waitFor(() => expect(utils.queryByText('opus session')).toBeNull())
      utils.store.dispatch(requestSlotReveal('k-opus'))
      await waitFor(() => expect(stored()).toEqual({ keys: [], exclude: false }))
      expect(utils.queryByText('opus session')).not.toBeNull()
    } finally {
      Element.prototype.scrollIntoView = original
    }
  })
})

describe('useSidebarModels — peer rows', () => {
  // The peer slot allowlist (`_clean_peer_slot`) carries no `model`, so a peer
  // row has no model datum. It must not read as Default (which would surface
  // every peer session under that row) and it is not "not X" either: a narrowing
  // filter hides it in both directions, and only an inactive filter shows it.
  const local: Slot[] = [{ key: 'k-fable', model: 'claude-fable-5' } as Slot]
  const peer = { key: 'remote-1', peer_id: 'inst-a' } as Slot
  const run = (keys: string[], exclude: boolean) =>
    renderHook(() => useSidebarModels({ filterModelKeys: new Set(keys), filterModelsExcluded: exclude, localSlots: local })).result.current

  it('keeps a withheld pin under its own model, since that is the session to move', () => {
    // The composer shows such a session as `auto` because the pin cannot run;
    // the filter is about what the session is pinned to, so the row stays.
    const withheld = { ...SLOTS[2], model_withheld: true } as unknown as Slot
    expect(slotModelKey(withheld)).toBe('claude-opus-5-5')
    expect(slotModelKey({ ...SLOTS[2], model_withheld: null } as unknown as Slot)).toBe('claude-opus-5-5')
    expect(slotModelKey(SLOTS[3] as Slot)).toBe(DEFAULT_MODEL_FILTER_KEY)
  })

  it('passes a peer row only while no model filter is active', () => {
    expect(run([], false).modelFilterPasses(peer)).toBe(true)
    expect(run(['fable-5-1m'], false).modelFilterPasses(peer)).toBe(false)
    expect(run(['fable-5-1m'], true).modelFilterPasses(peer)).toBe(false)
  })

  it('keeps the vocabulary to local rows, so a peer row adds no Default count', () => {
    expect(run([], false).modelFilterRows.map(r => [r.key, r.count])).toEqual([['fable-5-1m', 1]])
  })
})
