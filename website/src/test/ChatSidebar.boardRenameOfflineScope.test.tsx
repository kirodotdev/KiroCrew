/**
 * Board view renders one `SessionRow` per matching column, so a chat carrying
 * two tags appears twice. The offline rename refusal is keyed by slot AND
 * placement scope for that reason: keyed by slot alone it matches every copy,
 * every column grows a notice line, and the notices share one `data-testid`.
 *
 * The assertion is on the WHOLE document, not on one column, so a notice
 * leaking into the other column reddens rather than being queried past.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const rest: Record<string, unknown> = {}
      for (const [k, v] of Object.entries(props)) if (!FRAMER_PROPS.has(k)) rest[k] = v
      return React.createElement(tag, { ...rest, ref })
    })
  return {
    motion: new Proxy({}, { get: (_t, tag: string) => make(tag) }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => true,
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const SLOT_KEY = 'chat-two-tags-1'
const TITLE = 'Shared across two lanes'
const TAGS = [
  { id: 't1', name: 'Alpha', color: '#ff0000', order: 0, status: true },
  { id: 't2', name: 'Beta', color: '#00ff00', order: 1, status: true },
]
const COLUMNS = [
  { id: 'c1', name: 'Alpha lane', tag_ids: ['t1'], mode: 'any', order: 0 },
  { id: 'c2', name: 'Beta lane', tag_ids: ['t2'], mode: 'any', order: 1 },
]

const { tagColumnsMock, chatTagsMock, renameSlotMock } = vi.hoisted(() => ({
  tagColumnsMock: vi.fn(),
  chatTagsMock: vi.fn(),
  renameSlotMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'tagColumns') return tagColumnsMock
      if (prop === 'chatTags') return chatTagsMock
      if (prop === 'renameSlot') return renameSlotMock
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
import type { ChatSlot } from '../types'

const slot = {
  key: SLOT_KEY, title: TITLE, running: false, tags: ['t1', 't2'], created: '', last_ts: '',
} as unknown as ChatSlot

function renderBoard() {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon',
      sessionColorsIntensity: 'clear', slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], TAGS)
  qc.setQueryData(['tag-columns'], COLUMNS)
  qc.setQueryData(['chat-folders'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[slot]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, container: view.container }
}

describe('board view: the offline rename notice belongs to one column', () => {
  beforeEach(() => {
    tagColumnsMock.mockResolvedValue(COLUMNS)
    chatTagsMock.mockResolvedValue(TAGS)
    renameSlotMock.mockReset()
  })

  it('shows the refusal under the clicked column only, not under every copy', async () => {
    const { container } = renderBoard()

    // The setup is only meaningful if the slot really does render twice.
    await waitFor(() => {
      expect(container.querySelectorAll(`[data-slot-key="${SLOT_KEY}"]`).length).toBe(2)
    })
    const c1 = container.querySelector('[data-testid="column-c1"]') as HTMLElement
    const c2 = container.querySelector('[data-testid="column-c2"]') as HTMLElement
    expect(c1).toBeTruthy()
    expect(c2).toBeTruthy()

    const title = within(c1).getByTitle(TITLE)
    fireEvent.doubleClick(title, { detail: 2 })

    const selector = `[data-testid="session-rename-offline-${SLOT_KEY}"]`
    await waitFor(() => expect(container.querySelector(selector)).toBeTruthy())

    expect(container.querySelectorAll(selector).length).toBe(1)
    expect(within(c1).queryByTestId(`session-rename-offline-${SLOT_KEY}`)).toBeTruthy()
    expect(within(c2).queryByTestId(`session-rename-offline-${SLOT_KEY}`)).toBeNull()
    expect(renameSlotMock).not.toHaveBeenCalled()
  })

  it('refuses in whichever column was clicked, so the scope is read not assumed', async () => {
    const { container } = renderBoard()
    await waitFor(() => {
      expect(container.querySelectorAll(`[data-slot-key="${SLOT_KEY}"]`).length).toBe(2)
    })
    const c1 = container.querySelector('[data-testid="column-c1"]') as HTMLElement
    const c2 = container.querySelector('[data-testid="column-c2"]') as HTMLElement

    fireEvent.doubleClick(within(c2).getByTitle(TITLE), { detail: 2 })

    const selector = `[data-testid="session-rename-offline-${SLOT_KEY}"]`
    await waitFor(() => expect(container.querySelector(selector)).toBeTruthy())
    expect(container.querySelectorAll(selector).length).toBe(1)
    expect(within(c2).queryByTestId(`session-rename-offline-${SLOT_KEY}`)).toBeTruthy()
    expect(within(c1).queryByTestId(`session-rename-offline-${SLOT_KEY}`)).toBeNull()
  })
})
