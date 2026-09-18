/**
 * Regression tests for #10616 on the split-pane surface: ChatPane hosts its own
 * agent and model pickers, portaled to <body> and positioned from the composer
 * chip's rect. That rect used to be the chip's click-time snapshot, so the menu
 * detached from the chip whenever the composer moved under it (mobile keyboard
 * closing, composer growth, scroll). The pane's anchors now go through
 * `useAnchoredTriggerRect`, which keeps the trigger element ChatInput hands
 * over and re-reads it while the menu is open.
 *
 * Each test opens one picker for real, moves the chip, and fires the one signal
 * `window` events alone miss -- a `visualViewport` resize, how the iOS keyboard
 * announces itself. The menu must follow; pre-fix, nothing re-reads the chip.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([
      { model_name: 'auto', description: 'Models chosen by task' },
      { model_name: 'claude-opus-5', description: 'Claude Opus 5' },
    ]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }, { name: 'writer' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', agent: 'default', model: 'claude-opus-5', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={makeStore(slotKey)}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
}

/* A visualViewport double installed before render, so the picker anchors
 * subscribe to it; carries the fields `useVisualViewport` reads. */
let viewport: EventTarget
let originalViewport: PropertyDescriptor | undefined

beforeEach(() => {
  vi.clearAllMocks()
  originalViewport = Object.getOwnPropertyDescriptor(window, 'visualViewport')
  viewport = Object.assign(new EventTarget(), { height: window.innerHeight, offsetTop: 0 })
  Object.defineProperty(window, 'visualViewport', {
    configurable: true,
    value: viewport as unknown as VisualViewport,
  })
})

afterEach(() => {
  if (originalViewport) Object.defineProperty(window, 'visualViewport', originalViewport)
  else delete (window as { visualViewport?: VisualViewport }).visualViewport
})

/** Pin the chip's rect to a mutable value the test moves later. */
function anchorChip(chip: HTMLElement, y: number) {
  const state = { rect: DOMRect.fromRect({ x: 120, y, width: 96, height: 28 }) }
  vi.spyOn(chip, 'getBoundingClientRect').mockImplementation(() => state.rect)
  return {
    moveTo(nextY: number) { state.rect = DOMRect.fromRect({ x: 120, y: nextY, width: 96, height: 28 }) },
  }
}

/** The mobile keyboard closing: the chip moves, and only visualViewport says so. */
async function keyboardCloses(anchor: ReturnType<typeof anchorChip>, nextY: number) {
  anchor.moveTo(nextY)
  await act(async () => { fireEvent(viewport, new Event('resize')) })
}

describe('ChatPane composer pickers follow their chip while open (#10616)', () => {
  it('agent picker remeasures when the visual viewport changes', async () => {
    renderPane('pane-1')
    const chip = await waitFor(() => screen.getByTitle('Agent: default'))
    const anchor = anchorChip(chip, 600)

    await act(async () => { fireEvent.click(chip) })
    const menu = await waitFor(() => screen.getByRole('dialog', { name: 'Agent list' }))
    expect(menu.style.bottom).toBe(`${window.innerHeight - 600 + 4}px`)

    await keyboardCloses(anchor, 700)
    await waitFor(() => expect(menu.style.bottom).toBe(`${window.innerHeight - 700 + 4}px`))
  })

  it('model picker remeasures when the visual viewport changes', async () => {
    renderPane('pane-2')
    const chip = await waitFor(() => screen.getByTitle('Model: claude-opus-5'))
    const anchor = anchorChip(chip, 600)

    await act(async () => { fireEvent.click(chip) })
    const menu = await waitFor(() => screen.getByRole('dialog', { name: 'Model list' }))
    expect(menu.style.bottom).toBe(`${window.innerHeight - 600 + 4}px`)

    await keyboardCloses(anchor, 700)
    await waitFor(() => expect(menu.style.bottom).toBe(`${window.innerHeight - 700 + 4}px`))
  })
})
