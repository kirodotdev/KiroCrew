import { describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { render } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* Split-view chrome on the ChatPane root: the focus-dim overlay and the title
 * row's leading edge (#10585). Both are pinned here against the pane's own
 * markup, independent of the grid that hands them down. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'chat-1-stacking'

/** Shell overlays the dim must stay under: the sessions-drawer scrim (z-[46])
 *  and the mobile workspace panel (z-[47]) in ChatPage. */
const SHELL_OVERLAY_FLOOR = 46

function mount(props: Partial<React.ComponentProps<typeof ChatPane>> = {}) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, title: 'Stacking pane', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: '2026-09-01T00:00:00Z' }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: SLOT, slotState: 'idle', messages: [] } as unknown as RootState['chat'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChatPane slotKey={SLOT} {...props} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

/* In split view the single-chat title row is gone, and the shell's sessions
 * toggle keeps standing at the surface's top-left. The pane that owns that
 * corner takes it over through `leading`: on desktop it clears the shell's
 * stationary button (reserved column + the same hairline the title row
 * draws), on mobile it renders the toggle inline. Any other pane, and a pane
 * outside split view, starts its row at its own inset. */
describe('ChatPane focus dim', () => {
  it('never mounts the dim overlay outside split view', () => {
    const { container } = mount()
    expect(container.querySelector('[data-pane-dim]')).toBeNull()
  })

  it.each([true, false])('mounts the overlay in split view, dimmed unless focused: %s', focused => {
    const { container } = mount({ focused })
    const dim = container.querySelector('[data-pane-dim]') as HTMLElement
    expect(dim).not.toBeNull()
    expect(dim.dataset.paneDim).toBe(focused ? 'off' : 'on')
    // Above the pane's z-[1] / z-[2] message chrome, below every shell layer.
    const z = Number((dim.className.match(/\bz-(\d+)/) ?? [])[1])
    expect(z).toBeGreaterThan(2)
    expect(z).toBeLessThan(SHELL_OVERLAY_FLOOR)
  })
})

describe('ChatPane title row leading edge', () => {
  const row = (container: HTMLElement) => container.querySelector('[data-pane-title-row]') as HTMLElement

  it('starts at its own inset with no leading', () => {
    const { container } = mount()
    expect(row(container).className).toContain('pl-3')
    expect(row(container).querySelector('[data-pane-leading-divider]')).toBeNull()
  })

  it('reserves the shell toggle column and draws the divider for inset', () => {
    const { container } = mount({ leading: { inset: true } })
    expect(row(container).className).toContain('pl-[49px]')
    expect(row(container).className).not.toMatch(/\bpl-3\b/)
    expect(row(container).querySelector('[data-pane-leading-divider]')).not.toBeNull()
  })

  it('renders an inline leading control ahead of the title', () => {
    const { container, getByRole } = mount({ leading: { control: <button type="button" aria-label="toggle sessions" /> } })
    const control = getByRole('button', { name: 'toggle sessions' })
    expect(row(container).contains(control)).toBe(true)
    // Ahead of the title, so it reads as the row's first element.
    expect(control.compareDocumentPosition(row(container).querySelector('.truncate') as Element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(row(container).querySelector('[data-pane-leading-divider]')).toBeNull()
  })
})
