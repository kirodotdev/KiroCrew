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
const roster = vi.hoisted(() => ({ displayAgents: undefined as { name: string; session_color?: string }[] | undefined }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], displayAgents: roster.displayAgents, defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { ThemeProvider } from '../hooks/useTheme'

const SLOT = 'chat-1-stacking'

/** Shell overlays the dim must stay under: the sessions-drawer scrim (z-[46])
 *  and the mobile workspace panel (z-[47]) in pages/chat/page/. */
const SHELL_OVERLAY_FLOOR = 46

function mount(props: Partial<React.ComponentProps<typeof ChatPane>> = {}, slot: Record<string, unknown> = {}) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, title: 'Stacking pane', messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: '2026-09-01T00:00:00Z', ...slot }],
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
        <ThemeProvider>
        <MemoryRouter>
          <ChatPane slotKey={SLOT} {...props} />
        </MemoryRouter>
        </ThemeProvider>
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

describe('ChatPane title row session colour', () => {
  const row = (container: HTMLElement) => container.querySelector('[data-pane-title-row]') as HTMLElement
  const dot = (container: HTMLElement) => container.querySelector('[data-testid="chat-pane-status-dot"]') as HTMLElement

  it('keeps the neutral header and status dot for an uncoloured session', () => {
    const { container } = mount()
    expect(row(container).className).not.toContain('chat-header-colored')
    expect(dot(container).className).toContain('bg-accent')
  })

  it('tints the header and adds a session dot beside an unchanged status dot', () => {
    const { container } = mount({}, { color_hex: '#e11d48' })
    expect(row(container).className).toContain('chat-header-colored')
    expect(row(container).style.getPropertyValue('--session-color')).toBe('#e11d48')
    expect(dot(container).className).toContain('bg-accent')
    expect(dot(container).className).not.toContain('chat-header-dot')
    expect(container.querySelector('[data-testid="chat-pane-session-dot"]')).not.toBeNull()
  })

  it('has no session dot for an uncoloured session', () => {
    const { container } = mount()
    expect(container.querySelector('[data-testid="chat-pane-session-dot"]')).toBeNull()
  })

  it('keeps the agent colour while the scoped roster is empty after a scope switch', () => {
    roster.displayAgents = [{ name: 'tinted', session_color: '#2563eb' }]
    try {
      const { container } = mount({}, { agent: 'tinted' })
      expect(row(container).className).toContain('chat-header-colored')
      expect(row(container).style.getPropertyValue('--session-color')).toBe('#2563eb')
    } finally {
      roster.displayAgents = undefined
    }
  })
})
