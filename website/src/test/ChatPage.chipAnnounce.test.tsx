import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

// Screen-reader announcement for a composer file chip the RECONCILIATION
// un/restages on its own — a hand-edited or pasted @mention moves no focus, so
// a screen-reader user otherwise hears nothing (#14597). The ✕ button and a
// picker pick move focus already and must NOT announce (that would
// double-announce). This suite drives the three cases the issue names.

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    fileSearch: vi.fn().mockResolvedValue({
      root: '/repo',
      results: [
        { path: '/repo/src/main.ts', name: 'main.ts', size: 10, mtime: Math.floor(Date.now() / 1000) - 60, kind: 'file' },
        { path: '/repo/src/helper.ts', name: 'helper.ts', size: 10, mtime: Math.floor(Date.now() / 1000) - 60, kind: 'file' },
      ],
    }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/SidePanel', () => ({
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
  default: () => null,
}))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'
import { api } from '../api/client'

function makeStore(activeSlot: string, slots: { key: string; project?: string }[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slots: slots.map(s => ({ key: s.key, project: s.project, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
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

async function renderPage(store: ReturnType<typeof makeStore>) {
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

/** Type an @-token and pick main.ts from the file picker. Returns the textarea. */
async function pickMain() {
  const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
  fireEvent.change(ta, { target: { value: '@mai' } })
  const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
  fireEvent.mouseDown(row)
  await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
  await screen.findByLabelText('Remove')
  return ta
}

/** Pick a named file by typing a query and clicking its picker row. */
async function pickFile(ta: HTMLTextAreaElement, query: string, name: string, token: string) {
  fireEvent.change(ta, { target: { value: ta.value + '@' + query } })
  const row = await screen.findByText(name, undefined, { timeout: 3000 })
  fireEvent.mouseDown(row)
  await waitFor(() => expect(ta.value).toContain(token))
}

const announcer = () => screen.getByTestId('attachment-announcer')
/** The live-region text with the nonce's zero-width padding stripped. */
const announced = () => announcer().textContent?.replace(/\u200B/g, '') ?? ''

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(api.sendChat).mockClear()
})

describe('ChatPage composer file-chip announcements (#14597)', { timeout: 15_000 }, () => {
  it('starts with an empty live region and does not announce the picker pick', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    expect(announced()).toBe('')
    await pickMain()
    // A pick moves focus, so it is NOT the automatic path and must not announce.
    expect(announced()).toBe('')
  })

  it('announces the file name when a hand-edit unstages the chip', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickMain()
    // Break the @mention by hand (reconciliation unstages the chip).
    fireEvent.change(ta, { target: { value: 'please look' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    await waitFor(() => expect(announced()).toBe('main.ts removed from attachments'))
  })

  it('announces the file name when pasting the mention back restages the chip', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickMain()
    fireEvent.change(ta, { target: { value: 'please look' } })
    await waitFor(() => expect(announced()).toBe('main.ts removed from attachments'))
    // Paste the recorded mention back (reconciliation restages the chip).
    fireEvent.change(ta, { target: { value: 'please look @src/main.ts ' } })
    await screen.findByLabelText('Remove')
    await waitFor(() => expect(announced()).toBe('main.ts attached'))
  })

  it('does not announce when the ✕ button removes the chip (no double-announce)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickMain()
    fireEvent.change(ta, { target: { value: ta.value + 'explain' } })
    expect(announced()).toBe('')
    // The ✕ strips the token too, but it moves focus — it is NOT the automatic
    // path. The reconciliation that follows the strip must see the chip already
    // gone from state and announce nothing.
    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(ta.value).not.toContain('@src/main.ts'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    // The reconciliation after the strip has settled (chip and token both gone),
    // so had the ✕ routed through the announcer it would already read here.
    expect(announced()).toBe('')
    // Positive control that the announcer is live, not merely never written:
    // re-stage by picking, then hand-edit the mention away — the AUTOMATIC path
    // DOES announce. If the earlier ✕ had wrongly announced, this would show the
    // ✕'s message instead of the hand-edit's.
    await pickMain()
    fireEvent.change(ta, { target: { value: 'done' } })
    await waitFor(() => expect(announced()).toBe('main.ts removed from attachments'))
  })

  it('names BOTH files in ONE announcement when an edit restages and unstages at once', async () => {
    // The failure this pins (four reviewers flagged it): one committed edit can
    // produce both `revived` and `stale`. Two separate announce calls batch into
    // one render and the screen reader hears only the removal; the restore is
    // lost, and the nonce advancing twice can land on the same parity and never
    // fire. The fix joins both into ONE message / ONE announce call.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickMain()
    await pickFile(ta, 'help', 'helper.ts', '@src/helper.ts')
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    await waitFor(() => expect(ta.value).toContain('@src/helper.ts'))
    // One edit: break BOTH mentions at once → both unstage in a single pass.
    fireEvent.change(ta, { target: { value: 'please look' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    await waitFor(() => {
      const msg = announced()
      expect(msg).toContain('main.ts')
      expect(msg).toContain('helper.ts')
      expect(msg).toContain('removed from attachments')
    })
    // One edit: paste BOTH mentions back at once → both restage in a single pass.
    fireEvent.change(ta, { target: { value: 'please look @src/main.ts @src/helper.ts ' } })
    await waitFor(() => {
      const msg = announced()
      expect(msg).toContain('main.ts')
      expect(msg).toContain('helper.ts')
      expect(msg).toContain('attached')
    })
  })
})
