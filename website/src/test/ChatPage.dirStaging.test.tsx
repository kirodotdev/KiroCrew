import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
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
        { path: '/repo/src/widgets', name: 'widgets', size: 0, mtime: Math.floor(Date.now() / 1000) - 60, kind: 'dir' },
        { path: '/repo/src/main.ts', name: 'main.ts', size: 10, mtime: Math.floor(Date.now() / 1000) - 60, kind: 'file' },
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
  let result!: ReturnType<typeof render>
  await act(async () => {
    result = render(
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
  return result
}

/** Type an @-token, wait for the picker's folder row, click it. Returns the textarea. */
async function stageFolder() {
  const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
  fireEvent.change(ta, { target: { value: '@wid' } })
  // 200ms debounce before the search fires; findByText waits it out.
  const row = await screen.findByText('widgets/', undefined, { timeout: 3000 })
  fireEvent.mouseDown(row)
  // Chip render is the staging signal (remove control carries the aria-label).
  await screen.findByLabelText('Remove folder')
  return ta
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage staged folder references', { timeout: 15_000 }, () => {
  it('slot switch: staged folders stay with their slot draft (no cross-slot leak)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderPage(store)

    await stageFolder()

    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    // Chips derive from `@rel/` tokens in the composer text and drafts are
    // per-slot, so the incoming slot must not show the outgoing slot's chip…
    await waitFor(() => expect(screen.queryByLabelText('Remove folder')).not.toBeInTheDocument())

    // …and switching back restores the token with the text draft, so the chip
    // reappears with its one-click remove (the restore-path divergence fix).
    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    await screen.findByLabelText('Remove folder')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/widgets/')
  })

  it('removing the folder chip also strips its @-token from the composer', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)

    const ta = await stageFolder()
    expect(ta.value).toContain('@src/widgets/')

    fireEvent.click(screen.getByLabelText('Remove folder'))

    await waitFor(() => expect(screen.queryByLabelText('Remove folder')).not.toBeInTheDocument())
    // The remove control's promise: the agent no longer receives the folder.
    expect(ta.value).not.toContain('@src/widgets/')
  })

  it('token strip is exact: a longer sibling token survives the remove', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)

    const ta = await stageFolder()
    // User keeps typing after the pick, including a hand-typed longer token
    // that shares the staged token as a prefix. Both are folder references
    // now (chips derive from tokens), so two chips render.
    fireEvent.change(ta, { target: { value: ta.value + 'and @src/widgets/sub/ please' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove folder')).toHaveLength(2))

    // Remove the SHORTER one; the boundary-checked strip must not eat the
    // longer sibling that contains it as a prefix.
    fireEvent.click(screen.getAllByLabelText('Remove folder')[0])

    await waitFor(() => expect(ta.value).not.toMatch(/(^|\s)@src\/widgets\/(\s|$)/))
    expect(ta.value).toContain('@src/widgets/sub/')
    expect(screen.getAllByLabelText('Remove folder')).toHaveLength(1)
  })

  it('hand-editing the token out of the composer drops the orphaned chip', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)

    const ta = await stageFolder()
    expect(ta.value).toContain('@src/widgets/')

    // The composer token is the only payload the agent receives, so a chip
    // whose token was deleted by hand must not keep claiming the folder.
    fireEvent.change(ta, { target: { value: 'no folder here anymore' } })

    await waitFor(() => expect(screen.queryByLabelText('Remove folder')).not.toBeInTheDocument())
  })

  it('a hand-typed folder token stages its own chip (token presence is the source of truth)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderPage(store)

    const ta = await stageFolder()

    // Replacing the picked token with a DIFFERENT hand-typed one re-derives
    // the chip set from the text: the picked chip dies with its token, and
    // the typed token — which WILL serialize on send exactly like a picked
    // one — gets a chip with a working remove control.
    fireEvent.change(ta, { target: { value: 'look at @src/widgets/sub/ instead' } })

    await waitFor(() => expect(screen.getAllByLabelText('Remove folder')).toHaveLength(1))
    fireEvent.click(screen.getByLabelText('Remove folder'))
    await waitFor(() => expect(ta.value).not.toContain('@src/widgets/sub/'))
  })
})

describe('ChatPage folder serialization on send', { timeout: 15_000 }, () => {
  it('send rewrites the token to [attached_dir N] with the absolute path and carries meta.dirs', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = await stageFolder()
    fireEvent.change(ta, { target: { value: ta.value + 'summarize it' } })

    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    const call = vi.mocked(api.sendChat).mock.calls[0]
    const llmText = call[0] as string
    const meta = call[4] as Record<string, unknown> | undefined
    // The agent receives the absolute-path marker, never the display token.
    expect(llmText).toContain('[attached_dir 1] /repo/src/widgets')
    expect(llmText).not.toContain('@src/widgets/')
    // meta.dirs is the lossless index for replay rendering (marker N -> dirs[N-1]).
    expect(meta?.dirs).toEqual(['/repo/src/widgets'])
  })
})

describe('ChatPage file-chip remove parity', { timeout: 15_000 }, () => {
  it('removing a picker-picked file chip strips its inserted @-token from the composer', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: '@mai' } })
    const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(row)

    // The pick inserted the token and staged the file chip.
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const removeBtn = await screen.findByLabelText('Remove')

    // Removing the chip strips the token too — the same contract folder
    // chips have, so "remove" cannot mean different things per chip kind.
    fireEvent.click(removeBtn)
    await waitFor(() => expect(ta.value).not.toContain('@src/main.ts'))
  })

  it('token strip survives a remount: the restored draft has no pick-time ref', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    const first = await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: '@mai' } })
    const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(row)
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    // Reload: text + file drafts restore from storage, but the in-memory
    // pickedFileTokens ref is gone. The remove must DERIVE the token from
    // the composer text (buildRelMap walk) instead of silently keeping it.
    first.unmount()
    const store2 = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store2)
    const ta2 = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(ta2.value).toContain('@src/main.ts'))
    const removeBtn2 = await screen.findByLabelText('Remove')

    fireEvent.click(removeBtn2)
    await waitFor(() => expect(ta2.value).not.toContain('@src/main.ts'))
  })
})
