/**
 * Regression tests for #10616: the composer-toolbar pickers ChatPage hosts
 * (agent, model, project, app session control) portal to <body> and position
 * themselves from the chip's rect. That rect used to be a one-time snapshot
 * taken in the chip's click handler, so anything that moved the composer while
 * the menu stayed open -- the mobile keyboard closing, the composer growing, a
 * scroll -- left the menu floating where the chip used to be. PR #10608 fixed
 * the "+" menu, mic-source menu and Steer/Queue picker through
 * `useAnchorRemeasure`; these pickers keep their anchor state in the PAGE, so
 * they go through `useAnchoredTriggerRect`, which owns the trigger element the
 * chip now hands over and re-reads it while the menu is open.
 *
 * Each test opens one picker for real (a real ChatInput, a real click on the
 * chip), moves the chip, and fires the one signal `window` events alone miss:
 * a `visualViewport` resize, which is how the iOS keyboard announces itself.
 * The menu must follow. On the pre-fix code the first assertion passes and the
 * second never does -- nothing re-reads the chip after the click.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'

interface VirtuosoMockProps {
  data?: unknown[]
  itemContent: (index: number, item: unknown) => ReactNode
}
vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: VirtuosoMockProps) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
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
    agentDetail: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    agentResolvedModel: vi.fn().mockResolvedValue({ model: 'claude-opus-5' }),
    recentProjects: vi.fn().mockResolvedValue({ dirs: ['/home/user/proj-x'] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/home/user', parent: '/home', dirs: [] }),
    projectGit: vi.fn().mockRejectedValue(new Error('not a repo')),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    listApps: vi.fn().mockResolvedValue([]),
    appSessionStatus: vi.fn().mockResolvedValue({ state: 'none' }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }, { name: 'researcher' }], defaultAgent: 'kirocrew' }) }))
// One app-contributed session control, so the composer renders its chip and
// ChatPage mounts SessionControlHost against it. The control's bundle does not
// exist in this harness; the host's own load-failure notice renders inside the
// positioned dialog, which is all these tests measure.
vi.mock('../hooks/useSessionControls', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useSessionControls')>()
  return {
    ...actual,
    useSessionControls: () => ({
      controls: [{
        key: 'test-app:scope',
        appName: 'test-app',
        appDisplayName: 'Test App',
        appVersion: '0.1.0',
        id: 'scope',
        entryPoint: 'dist/session-control.mjs',
        label: 'Scope',
        icon: 'Tag',
        allowedApi: [],
        allowedEvents: [],
        statusPath: '',
        processBacked: false,
      }],
      error: null,
    }),
  }
})
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [{ key: 'slot-a', messages: 0, running: false, mode: '', agent: 'kirocrew', model: 'claude-opus-5', project: '/home/user/old-proj', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: 'slot-a', messages: [],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderChat() {
  const store = makeStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

/* A visualViewport double: an EventTarget carrying the two fields the page's
 * own `useVisualViewport` reads, installed before render so both that hook and
 * the picker anchors subscribe to it. */
let viewport: EventTarget
let originalViewport: PropertyDescriptor | undefined

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
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
    get top() { return state.rect.top },
  }
}

/** The mobile keyboard closing: the chip moves, and only visualViewport says so. */
async function keyboardCloses(anchor: ReturnType<typeof anchorChip>, nextY: number) {
  anchor.moveTo(nextY)
  await act(async () => { fireEvent(viewport, new Event('resize')) })
}

describe('ChatPage composer pickers follow their chip while open (#10616)', { timeout: 15_000 }, () => {
  it('agent dropdown remeasures when the visual viewport changes', async () => {
    await renderChat()
    const chip = screen.getByTitle('Agent: kirocrew')
    const anchor = anchorChip(chip, 600)

    await act(async () => { fireEvent.click(chip) })
    const menu = await waitFor(() => screen.getByRole('dialog', { name: 'Agent selector' }))
    expect(menu.style.bottom).toBe(`${window.innerHeight - 600 + 4}px`)

    await keyboardCloses(anchor, 700)
    await waitFor(() => expect(menu.style.bottom).toBe(`${window.innerHeight - 700 + 4}px`))
  })

  it('model dropdown remeasures when the visual viewport changes', async () => {
    await renderChat()
    const chip = screen.getByTitle('Model: claude-opus-5')
    const anchor = anchorChip(chip, 600)

    await act(async () => { fireEvent.click(chip) })
    const menu = await waitFor(() => screen.getByRole('dialog', { name: 'Model list' }))
    expect(menu.style.bottom).toBe(`${window.innerHeight - 600 + 4}px`)

    await keyboardCloses(anchor, 700)
    await waitFor(() => expect(menu.style.bottom).toBe(`${window.innerHeight - 700 + 4}px`))
  })

  it('project picker remeasures when the visual viewport changes', async () => {
    await renderChat()
    const chip = screen.getByTitle('Project: /home/user/old-proj')
    // Low in the viewport, so the picker flips up and anchors on the chip's top.
    const anchor = anchorChip(chip, 600)

    await act(async () => { fireEvent.click(chip) })
    const row = await waitFor(() => screen.getByRole('option', { name: /proj-x/ }))
    const menu = row.closest('div.fixed') as HTMLElement
    expect(menu).toBeInstanceOf(HTMLElement)
    expect(menu.style.bottom).toBe(`${window.innerHeight - 600 + 4}px`)

    await keyboardCloses(anchor, 700)
    await waitFor(() => expect(menu.style.bottom).toBe(`${window.innerHeight - 700 + 4}px`))
  })

  it('session-control popover remeasures when the visual viewport changes', async () => {
    await renderChat()
    const chip = await waitFor(() => screen.getByRole('button', { name: 'Scope' }))
    const anchor = anchorChip(chip, 600)

    await act(async () => { fireEvent.click(chip) })
    const menu = await waitFor(() => screen.getByRole('dialog', { name: /Scope/ }))
    expect(menu.style.bottom).toBe(`${window.innerHeight - 600 + 6}px`)

    await keyboardCloses(anchor, 700)
    await waitFor(() => expect(menu.style.bottom).toBe(`${window.innerHeight - 700 + 6}px`))
  })
})
