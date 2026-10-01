import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { openActivityPanel } from '../store/chatSlice'
import dashboardReducer, { updateSlot } from '../store/dashboardSlice'
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
// Minimal SidePanel stand-in driving handleAddToContext's FILE branch (the
// tree's "Add to chat" action) for two files whose rels are `report` and
// `report,` -- both legal names, the shorter a consumable prefix of the longer.
vi.mock('../pages/chat/SidePanel', () => ({
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
  default: ({ onAddToContext }: { onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void }) => (
    <>
      <div><button onClick={() => onAddToContext?.('/repo/report', 'file')}>Add to chat: report</button></div>
      <div><button onClick={() => onAddToContext?.('/repo/report,', 'file')}>Add to chat: report,</button></div>
      <div><button onClick={() => onAddToContext?.('/repo/src/main.ts', 'file')}>Add to chat: main.ts</button></div>
    </>
  ),
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

/** Type an @-token and pick main.ts from the file picker. Returns the textarea. */
async function pickFile() {
  const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
  fireEvent.change(ta, { target: { value: '@mai' } })
  const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
  fireEvent.mouseDown(row)
  await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
  await screen.findByLabelText('Remove')
  return ta
}

const undo = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
const redo = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true, shiftKey: true })

async function send(ta: HTMLTextAreaElement) {
  await act(async () => { fireEvent.keyDown(ta, { key: 'Enter' }) })
  await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
  const call = vi.mocked(api.sendChat).mock.calls.at(-1)!
  return call[0] as string
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  vi.mocked(api.sendChat).mockClear()
})

describe('ChatPage file chip remove + undo', { timeout: 15_000 }, () => {
  it('undo after removing a picked file chip brings the attachment back and sends it', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: ta.value + 'explain' } })

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(ta.value).not.toContain('@src/main.ts'))
    expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument()

    undo(ta)
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    // The chip is back, and its remove control strips the token again.
    await screen.findByLabelText('Remove')

    const llm = await send(ta)
    expect(llm).toContain('[attached_file 1] /repo/src/main.ts')
  })

  it('a removed file chip that is not undone is not sent', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: ta.value + 'explain' } })

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(ta.value).not.toContain('@src/main.ts'))

    const llm = await send(ta)
    expect(llm).not.toContain('/repo/src/main.ts')
  })

  it('redo after the undo removes the attachment again', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: ta.value + 'explain' } })

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(ta.value).not.toContain('@src/main.ts'))
    undo(ta)
    await screen.findByLabelText('Remove')
    redo(ta)
    await waitFor(() => expect(ta.value).not.toContain('@src/main.ts'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())

    const llm = await send(ta)
    expect(llm).not.toContain('/repo/src/main.ts')
  })

  it('removing a chip whose token appears twice does not re-stage the file', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    // The strip leaves one of two adjacent copies behind; that leftover must
    // not read as the token coming back.
    fireEvent.change(ta, { target: { value: '@src/main.ts @src/main.ts ' } })
    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    await new Promise(r => setTimeout(r, 50))
    expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument()

    const llm = await send(ta)
    expect(llm).not.toContain('/repo/src/main.ts')
  })

  it('a send drops removed chips: the token typed afterwards does not reattach', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: ta.value + 'explain' } })
    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(ta.value).not.toContain('@src/main.ts'))
    await send(ta)
    await waitFor(() => expect(ta.value).toBe(''))

    fireEvent.change(ta, { target: { value: 'see @src/main.ts ' } })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument()
  })

  it('undo revives a removed chip whose rel is a consumable prefix of a staged sibling\'s (fork Opus review)', async () => {
    // `report` and `report,` are both legal, distinct filenames. With both
    // staged and each holding its own mention, removing the SHORTER one
    // strips only its bare `@report ` (the strict boundary protects the
    // sibling's `@report,`). The survival check must apply the SAME sibling
    // rule: read under the permissive boundary, the sibling's surviving
    // `@report,` looked like `report` still mentioned, the aliases were
    // dropped, and undo brought the text back without the chip.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report,'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    fireEvent.click(await screen.findByText('Add to chat: report'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'see @report, and @report here' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))

    // Remove the SHORTER file (staged second, chip index 1).
    fireEvent.click(screen.getAllByLabelText('Remove')[1])
    await waitFor(() => expect(ta.value).toBe('see @report, and here'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))

    undo(ta)
    await waitFor(() => expect(ta.value).toBe('see @report, and @report here'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))

    const llm = await send(ta)
    expect(llm).toContain('/repo/report,')
    expect(llm).toMatch(/\[attached_file \d\] \/repo\/report(?=\s|$)/m)
  })

  it('an old project\'s alias left behind by the strip does not cost the chip its undo (fork GPT review)', async () => {
    // Picked under /repo, the file is recorded as `@src/main.ts`; after the
    // project moves to /repo/src a re-pick adds `@main.ts`. A hand-typed
    // duplicate of the OLD spelling that the strip cannot reach survives the
    // ✕. The reconciliation never revives off an old project's alias, so the
    // survival check must not drop the aliases for it either: undo has to
    // bring back the current mention AND its attachment.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    act(() => { store.dispatch(openActivityPanel()) })
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    act(() => { store.dispatch(updateSlot({ key: 'slot-a', project: '/repo/src' })) })
    fireEvent.change(ta, { target: { value: '' } })
    fireEvent.click(screen.getByText('Add to chat: main.ts'))
    await waitFor(() => expect(ta.value).toMatch(/(^|\s)@main\.ts/))
    fireEvent.change(ta, { target: { value: 'see @main.ts and @src/main.ts @src/main.ts ' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(ta.value).not.toMatch(/@main\.ts/))
    expect(ta.value).toContain('@src/main.ts')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())

    undo(ta)
    await waitFor(() => expect(ta.value).toMatch(/(^|\s)@main\.ts/))
    await screen.findByLabelText('Remove')

    const llm = await send(ta)
    expect(llm).toContain('/repo/src/main.ts')
  })

  /** Stage `report,` then `report` through the tree's "Add to chat", with both
   *  mentioned in the text, then unmount and remount the page the way a reload
   *  does (sessionStorage survives). Returns the fresh textarea. */
  async function stagePrefixPairAndReload(store: ReturnType<typeof makeStore>, view: ReturnType<typeof render>) {
    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report,'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    fireEvent.click(await screen.findByText('Add to chat: report'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'see @report, and @report here' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    await new Promise(r => setTimeout(r, 600))
    view.unmount()
    await renderPage(store)
    const fresh = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(fresh.value).toBe('see @report, and @report here'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    return fresh
  }

  it('after a reload, undo still revives a removed chip beside its prefix sibling (fork GPT review)', async () => {
    // The staged files survive a reload through the file drafts; the aliases
    // must too, or the restored `report,` protects nothing, its `@report,`
    // reads as `report` still mentioned, and the ✕ drops `report`'s aliases.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    const view = await renderPage(store)
    const ta = await stagePrefixPairAndReload(store, view)

    fireEvent.click(screen.getAllByLabelText('Remove')[1])
    await waitFor(() => expect(ta.value).toBe('see @report, and here'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))

    undo(ta)
    await waitFor(() => expect(ta.value).toBe('see @report, and @report here'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))

    const llm = await send(ta)
    expect(llm).toContain('/repo/report,')
    expect(llm).toMatch(/\[attached_file \d\] \/repo\/report(?=\s|$)/m)
  })

  it('after a reload, deleting a restored chip\'s mention by hand unstages it', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    const view = await renderPage(store)
    const ta = await stagePrefixPairAndReload(store, view)

    fireEvent.change(ta, { target: { value: 'see @report, and here' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))

    const llm = await send(ta)
    expect(llm).toContain('/repo/report,')
    expect(llm).not.toMatch(/\/repo\/report(?=\s|$)/m)
  })
})
