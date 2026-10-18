/**
 * The sort-and-filter menu's two mutually exclusive groups -- "Sort sessions by"
 * (six rows) and "Folder order" (three rows) -- must declare their state to a
 * screen reader, not only to the eye. Before this was fixed the rows were plain
 * menu items whose sole active-state cue was a check icon, so assistive tech
 * announced nine identical stateless items and never said which sort or which
 * folder order was in effect (#14178).
 *
 * Each row is now a `menuitemradio` carrying `aria-checked`: exactly the selected
 * row is `aria-checked="true"`, every sibling `"false"`. The visible check mark
 * stays (sighted cue), hidden from the a11y tree.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
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
const patchConfig = vi.fn(async (path: string, value: unknown) => {
  if (path === 'dashboard.folder_sort') serverConfig.dashboard.folder_sort = value
  if (path === 'dashboard.session_sort') serverConfig.dashboard.session_sort = value
  return { ok: true }
})
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
import type { ChatFolder, ChatSlot } from '../types'

const FOLDERS: ChatFolder[] = [
  { id: 'f1', name: 'Alpha', order: 0, collapsed: true, created_at: 1_000 },
  { id: 'f2', name: 'Bravo', order: 1, collapsed: true, created_at: 2_000 },
]

function renderSidebar(dashboard: Record<string, unknown>) {
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
  serverConfig.dashboard = { ...dashboard }
  qc.setQueryData(['kirocrewConfig'], { dashboard: { ...dashboard } })
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

/** aria-checked of every menuitemradio whose accessible row contains `label`. */
function radioStateByText(getAllByRole: (r: string) => HTMLElement[], label: string): string | null {
  const row = getAllByRole('menuitemradio').find(el => (el.textContent ?? '').includes(label))
  return row ? row.getAttribute('aria-checked') : null
}

beforeEach(() => { localStorage.clear(); patchConfig.mockClear(); kirocrewConfig.mockClear() })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — sort-and-filter menu announces the checked row (#14178)', () => {
  it('renders every Folder order row as a menuitemradio, checked only on the active mode', async () => {
    const { getByLabelText, findByTestId, getByTestId } = renderSidebar({ folder_sort: 'name' })
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await findByTestId('folder-order-name')

    for (const mode of ['custom', 'name', 'created']) {
      const row = getByTestId(`folder-order-${mode}`)
      expect(row.getAttribute('role')).toBe('menuitemradio')
    }
    expect(getByTestId('folder-order-name').getAttribute('aria-checked')).toBe('true')
    expect(getByTestId('folder-order-custom').getAttribute('aria-checked')).toBe('false')
    expect(getByTestId('folder-order-created').getAttribute('aria-checked')).toBe('false')
  })

  it('renders every "Sort sessions by" row as a menuitemradio, checked only on the active sort', async () => {
    const { getByLabelText, findByTestId, getAllByRole } = renderSidebar({ session_sort: 'name-asc' })
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await findByTestId('folder-order-custom')

    // The session-sort rows carry no test ids, so find them by their visible
    // labels among the menuitemradios. Exactly the active sort is checked.
    const radios = getAllByRole('menuitemradio')
    // Ten mutually exclusive rows in all: seven session sorts (Manual among
    // them) + three folder orders.
    expect(radios.length).toBe(10)
    const checked = radios.filter(r => r.getAttribute('aria-checked') === 'true')
    // One per group: the active session sort and the (default) custom folder order.
    expect(checked.length).toBe(2)

    // The chosen session sort ("A → Z") announces checked; another sort does not.
    expect(radioStateByText(getAllByRole, 'A → Z')).toBe('true')
    expect(radioStateByText(getAllByRole, 'Oldest')).toBe('false')
  })

  it('the visible check mark is hidden from assistive tech (aria-checked carries the state)', async () => {
    const { getByLabelText, findByTestId, getByTestId } = renderSidebar({ folder_sort: 'created' })
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await findByTestId('folder-order-created')

    const active = getByTestId('folder-order-created')
    const check = active.querySelector('svg.text-accent')
    expect(check).toBeTruthy()
    expect(check?.getAttribute('aria-hidden')).toBe('true')
    // The row that is not active carries no check icon at all.
    expect(getByTestId('folder-order-custom').querySelector('svg.text-accent')).toBeNull()
  })

  it('a Sort sessions by pick writes through the row onSelect, not the group onValueChange', async () => {
    // Regression guard: the session sort is drawn from an OPTIMISTIC overlay, so
    // the radio group's `value` already reflects an in-flight pick. Radix fires
    // `onValueChange` only on a change against that value, which would drop a
    // re-pick (and its carry-over side effect). The write therefore lives on each
    // row's `onSelect`; picking a row must reach the config write regardless.
    const { getByLabelText, findByTestId, getAllByRole } = renderSidebar({ session_sort: 'name-asc' })
    fireEvent.keyDown(getByLabelText('Sort and filter sessions'), { key: 'Enter' })
    await findByTestId('folder-order-custom')

    const oldest = getAllByRole('menuitemradio').find(el => (el.textContent ?? '').includes('Oldest'))!
    fireEvent.click(oldest)
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('dashboard.session_sort', 'date-asc'))
  })
})
