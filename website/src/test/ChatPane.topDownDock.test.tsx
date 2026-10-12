// Feature: the opt-in top-down chat layout in ChatPane (Settings > Chat >
// `topDownLayout`).
//
// The composer dock keeps the scroll-under-glass decision
// (docs/decisions/2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md)
// at the other edge: the dock root stays `absolute` over a full-height scroller,
// anchored to the scroller's TOP edge, and the scroller pays for the covered
// strip with `padding-top`. A crewmate's chat (the Members page DM) keeps the
// bottom layout: its unread landing reads the bottom geometry.
//
// happy-dom has no layout, so the two measured inputs are stubbed on the
// prototype: the dock root's `offsetHeight` (the shared dock measurement) and
// the scroller's `offsetTop` (the pane's title row above it).

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, act } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

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

// Desktop width: the top-down layout is off under the 768px breakpoint.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

const SLOT = 'pane-top-down-dock'
const CHAT_CONFIG_KEY = 'mc-chat-config'
const DOCK_H = 120
const TITLE_ROW_H = 41
// DOCK_CLEARANCE_PX (pages/chat/composerDockMetrics.ts).
const CLEARANCE = 16

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

async function renderPane(props: Partial<React.ComponentProps<typeof ChatPane>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let container!: HTMLElement
  await act(async () => {
    container = render(
      <Provider store={makeStore()}>
        <QueryClientProvider client={qc}>
          <ThemeProvider>
            <MemoryRouter>
              <ChatPane slotKey={SLOT} {...props} />
            </MemoryRouter>
          </ThemeProvider>
        </QueryClientProvider>
      </Provider>,
    ).container
  })
  const scroller = container.querySelector('.chat-container') as HTMLElement
  const dock = container.querySelector('[data-testid="composer-dock-root"]') as HTMLElement
  expect(scroller).not.toBeNull()
  expect(dock).not.toBeNull()
  return { container, scroller, dock }
}

type Saved = { name: 'offsetHeight' | 'offsetTop'; desc: PropertyDescriptor | undefined }
let saved: Saved[] = []

function stubGeometry() {
  saved = (['offsetHeight', 'offsetTop'] as const).map(name => ({ name, desc: Object.getOwnPropertyDescriptor(HTMLElement.prototype, name) }))
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get(this: HTMLElement) { return this.getAttribute('data-testid') === 'composer-dock-root' ? DOCK_H : 0 },
  })
  Object.defineProperty(HTMLElement.prototype, 'offsetTop', {
    configurable: true,
    get(this: HTMLElement) { return this.classList.contains('chat-container') ? TITLE_ROW_H : 0 },
  })
}

function restoreGeometry() {
  for (const { name, desc } of saved) {
    if (desc) Object.defineProperty(HTMLElement.prototype, name, desc)
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name]
  }
  saved = []
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: true }))
  stubGeometry()
})

afterEach(() => {
  restoreGeometry()
  localStorage.removeItem(CHAT_CONFIG_KEY)
})

describe('ChatPane top-down layout: the dock floats over the scroller top', () => {
  it('anchors the dock root to the scroller top edge and pays for it with top padding', async () => {
    const { container, scroller, dock } = await renderPane()
    // Out of flow over the scroller, anchored to the top: the scroller's top in
    // the pane (under the title row) plus no host band.
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'pointer-events-none']))
    expect(dock.className.split(/\s+/)).not.toContain('bottom-0')
    expect(dock.style.top).toBe(`${TITLE_ROW_H}px`)
    // The covered strip is top padding; the bottom keeps the clearance only.
    expect(scroller.style.paddingTop).toBe(`${DOCK_H + CLEARANCE}px`)
    expect(scroller.style.paddingBottom).toBe(`${CLEARANCE}px`)
    // No in-flow slot ends the scroller's box at the dock: the dock is a
    // sibling AFTER the scroller (so it paints over it) in the same pane root.
    expect(container.querySelector('[data-testid="chat-pane-top-dock-slot"]')).toBeNull()
    expect(dock.parentElement).toBe(scroller.parentElement)
    expect(dock.compareDocumentPosition(scroller) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy()
  })

  it('pays a host band over the pane top once: the dock sits under it and the scroller pads it', async () => {
    const { scroller, dock } = await renderPane({ topInset: 48 })
    expect(dock.style.top).toBe(`${TITLE_ROW_H + 48}px`)
    expect(scroller.style.paddingTop).toBe(`${48 + DOCK_H + CLEARANCE}px`)
  })

  it('keeps a crewmate pane on the bottom layout', async () => {
    const { scroller, dock } = await renderPane({ crewmate: { name: 'Radar' } })
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0', 'pointer-events-none']))
    expect(dock.style.top).toBe('')
    expect(scroller.style.paddingTop).toBe('12px')
    expect(scroller.style.paddingBottom).toBe(`${DOCK_H + CLEARANCE}px`)
  })

  it('keeps the bottom layout with the setting off', async () => {
    localStorage.setItem(CHAT_CONFIG_KEY, JSON.stringify({ topDownLayout: false }))
    const { scroller, dock } = await renderPane()
    expect(dock.className.split(/\s+/)).toEqual(expect.arrayContaining(['absolute', 'bottom-0', 'pointer-events-none']))
    expect(dock.style.top).toBe('')
    expect(scroller.style.paddingTop).toBe('12px')
    expect(scroller.style.paddingBottom).toBe(`${DOCK_H + CLEARANCE}px`)
  })
})
