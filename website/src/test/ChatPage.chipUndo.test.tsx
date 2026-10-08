import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor, createEvent } from '@testing-library/react'
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
      <div><button onClick={() => onAddToContext?.('/repo/report final.pdf', 'file')}>Add to chat: report final.pdf</button></div>
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

  // #14675: a Backspace at the end of a staged `@mention` must remove the
  // WHOLE mention (and unstage its chip), not one character -- otherwise the
  // text keeps a half-reference (`@src/main.t`) and the message is sent naming
  // a file with no attachment. FAILS on origin/main: with no atomic-delete
  // handler the Backspace is not consumed, so the full `@src/main.ts` mention
  // stays in the text (and its chip stays staged). Passes with the fix.
  it('Backspace at the end of a staged mention removes the whole mention atomically (#14675)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    // Reproduce the issue's exact steps: `please review @src/main.ts for the bug`.
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    // Caret right after `@src/main.ts` (before the following space).
    const caret = 'please review @src/main.ts'.length
    ta.setSelectionRange(caret, caret)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace' }) })

    // The whole mention is gone -- no half-reference left behind.
    await waitFor(() => expect(ta.value).not.toContain('@src/main.t'))
    expect(ta.value).not.toContain('@src/main.ts')
    expect(ta.value).toBe('please review for the bug')
    // The chip unstages with it, and the send carries no attachment.
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    const llm = await send(ta)
    expect(llm).not.toContain('@src/main.t')
    expect(llm).not.toMatch(/\[attached_file \d\]/)
  })

  // #14675 (GPT 6.1 review): when one staged alias is a space-boundary prefix
  // of another (`report` vs `report final.pdf`), the atomic delete must match
  // the WHOLE mention at the caret, not let the shorter alias `report` match
  // inside `@report final.pdf` (where the space after `report` is a valid
  // mention boundary). Deleting inside the longer mention must remove it
  // whole, not leave `final.pdf` behind and drop the longer file's chip.
  // FAILS without the longest-first sort: the short `report` wins the scan.
  it('atomic delete picks the whole mention, not a shorter space-boundary prefix sibling (#14675, GPT 6.1 review)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    fireEvent.click(await screen.findByText('Add to chat: report final.pdf'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'see @report and @report final.pdf please' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    // Caret just past the `@report` prefix INSIDE the longer `@report
    // final.pdf` mention -- the exact spot where the short alias `report`
    // (space-boundary) would wrongly match without the longest-first sort.
    const caret = 'see @report and @report'.length
    ta.setSelectionRange(caret, caret)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace' }) })

    // The whole longer mention is gone -- NOT reduced to `final.pdf` by a
    // short-alias match -- and the standalone `@report` is untouched.
    await waitFor(() => expect(ta.value).toBe('see @report and please'))
    expect(ta.value).not.toContain('final.pdf')
    expect(ta.value).toContain('@report ')
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
  })

  // The mirror of the above for the forward Delete key, caret just BEFORE the
  // `@`. Same atomic removal, same chip unstage.
  it('Delete just before a staged mention removes the whole mention atomically (#14675)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const caret = 'please review '.length // just before the `@`
    ta.setSelectionRange(caret, caret)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Delete' }) })

    await waitFor(() => expect(ta.value).not.toContain('@src/main.ts'))
    expect(ta.value).toBe('please review for the bug')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // #14675 (crew-pr-reviewer): a word-delete chord (Ctrl/Alt+Backspace) next to
  // a staged mention must remove the WHOLE mention, not the native word-back
  // delete that leaves `@src/main.` -- the same half-reference the plain key
  // avoids. Mirrors the paste-token atom's word-delete handling.
  it('Ctrl+Backspace (word delete) next to a staged mention removes the whole mention (#14675, crew-pr-reviewer)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const caret = 'please review @src/main.ts'.length // just past the mention
    ta.setSelectionRange(caret, caret)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace', ctrlKey: true }) })

    expect(ta.value).toBe('please review for the bug')
    expect(ta.value).not.toContain('@src/main.')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // #14675 (crew-pr-reviewer, 22:12 review): a word-delete chord with the caret
  // INSIDE the mention must take the whole mention, the same as a plain
  // Backspace inside it does -- the two paths must agree, else Ctrl+Backspace
  // mid-mention falls through to the native word delete that leaves `@src/main.`.
  it('Ctrl+Backspace (word delete) with the caret inside a staged mention removes the whole mention (#14675, crew-pr-reviewer)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const caret = 'please review @src/ma'.length // strictly inside the mention
    ta.setSelectionRange(caret, caret)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace', ctrlKey: true }) })

    expect(ta.value).toBe('please review for the bug')
    expect(ta.value).not.toContain('@src/main.')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // #14675 (crew-pr-reviewer, 22:12 review): a word delete deletes back across
  // whitespace to the token, so Ctrl+Backspace with the caret AFTER the
  // mention's trailing space must treat that space as adjacent and take the
  // whole mention (and the space), not leave the half-reference `@src/main.`.
  it('Ctrl+Backspace (word delete) after the mention\'s trailing space removes the whole mention (#14675, crew-pr-reviewer)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const caret = 'please review @src/main.ts '.length // just past the trailing space
    ta.setSelectionRange(caret, caret)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace', ctrlKey: true }) })

    expect(ta.value).toBe('please review for the bug')
    expect(ta.value).not.toContain('@src/main.')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // #14675 (crew-pr-reviewer): Shift+Backspace is a plain Backspace -- Shift
  // alone must not fall through to the native one-character edit that leaves a
  // half-reference whose chip then unstages.
  it('Shift+Backspace at the end of a staged mention removes the whole mention (#14675, crew-pr-reviewer)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const caret = 'please review @src/main.ts'.length
    ta.setSelectionRange(caret, caret)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace', shiftKey: true }) })

    expect(ta.value).toBe('please review for the bug')
    expect(ta.value).not.toContain('@src/main.')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // #14675 (crew-pr-reviewer / Opus, 06:04 review): Shift+Delete over a
  // selection is the native Cut chord on Windows/Linux. The atomic handler
  // must NOT claim it -- preventDefault-ing the cut would delete the selection
  // and the mention while nothing reaches the clipboard, so the user loses the
  // text. The keydown must fall through (not prevented), the handler returns
  // null, and the mention stays (jsdom performs no real cut; the point is the
  // handler does not swallow the chord). Cut is the deferred #18263 seam.
  it('Shift+Delete over a selection is left to the native cut, not swallowed (#14675, Opus review)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    // Select across the mention (`review @src/main.ts`) -- a selection, which
    // is what makes Shift+Delete the Cut chord.
    const selStart = 'please '.length
    const selEnd = 'please review @src/main.ts'.length
    ta.setSelectionRange(selStart, selEnd)
    let prevented = true
    await act(async () => {
      const ev = createEvent.keyDown(ta, { key: 'Delete', shiftKey: true })
      fireEvent(ta, ev)
      prevented = ev.defaultPrevented
    })

    // The handler returned null, so the keydown was NOT prevented and the
    // mention text is untouched -- the browser's cut will handle the selection.
    expect(prevented).toBe(false)
    expect(ta.value).toContain('@src/main.ts')
    expect(screen.queryByLabelText('Remove')).toBeInTheDocument()
  })
  // mention must still delete the WHOLE mention, not slice it to a half-
  // reference. Select from mid-text into the middle of `@src/main.ts` and press
  // Backspace -- the entire mention goes, and its chip unstages.
  it('a selection partly overlapping a staged mention deletes the whole mention (#14675, crew-pr-reviewer)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    // Select from inside the word "review" to the middle of the mention
    // (`@src/ma|in.ts`) -- a partial cover of the mention.
    const selStart = 'please re'.length
    const selEnd = 'please review @src/ma'.length
    ta.setSelectionRange(selStart, selEnd)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace' }) })

    // No `@src/...` fragment survives, and the chip unstages.
    expect(ta.value).not.toContain('@src/ma')
    expect(ta.value).not.toContain('@src/main.ts')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // #14675 (GPT 6.1 review): a selection inside the LONGER of a prefix-sibling
  // pair (`@report` is a space-boundary prefix of `@report final.pdf`) must
  // remove the WHOLE longer mention. Both spans share the `@report` start, so
  // the shorter one can sort last on `at`; spanning its end would truncate the
  // longer mention to `final.pdf` and drop the longer file's chip. The removal
  // must span the furthest end across all overlapping hits.
  it('a selection inside the longer of a prefix-sibling pair removes the whole longer mention, not a truncation (#14675, GPT 6.1 review)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    const view = await renderPage(store)
    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    fireEvent.click(await screen.findByText('Add to chat: report final.pdf'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'see @report and @report final.pdf please' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    // Select `@rep` INSIDE the second (longer) mention -- a partial cover whose
    // span starts at the shared `@report` start.
    const selStart = 'see @report and '.length // the `@` of the longer mention
    const selEnd = 'see @report and @rep'.length
    ta.setSelectionRange(selStart, selEnd)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace' }) })

    // The whole longer mention is gone -- NOT truncated to `final.pdf` -- and
    // the standalone `@report` is untouched. (A selection removes the mention
    // whole but does not dedupe the surrounding spaces, so the collapsed gap
    // is two spaces -- the point is that no `final.pdf` fragment survives.)
    await waitFor(() => expect(ta.value).toBe('see @report and  please'))
    expect(ta.value).not.toContain('final.pdf')
    expect(ta.value).toContain('@report ')
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    void view
  })

  // #14675 (crew-pr-reviewer): while the prompt optimizer runs, the textarea is
  // readOnly and must not accept a chip edit -- a Backspace next to a staged
  // mention must NOT unstage it, or the optimizer silently discards its result
  // against a now-shorter draft. The keydown handler skips the mention branch
  // when `optimizingRef.current` is set.
  it('a Backspace next to a staged mention is ignored while the optimizer runs (#14675, crew-pr-reviewer)', async () => {
    // Hold the optimize request open so the composer stays in its readOnly
    // optimizing state across the Backspace.
    let release!: (v: unknown) => void
    const pending = new Promise(res => { release = res })
    const realFetch = globalThis.fetch
    vi.stubGlobal('fetch', (url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return pending.then(() => ({ ok: true, json: async () => ({ changed: false, optimized: null }) }))
      }
      return Promise.resolve({ ok: true, json: async () => [] } as unknown as Response)
    })
    try {
      const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
      await renderPage(store)
      const ta = await pickFile()
      fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
      await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
      // Enter the optimizing (readOnly) state and wait for it.
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' })) })
      await waitFor(() => expect(ta).toHaveAttribute('readonly'))

      const caret = 'please review @src/main.ts'.length
      ta.setSelectionRange(caret, caret)
      await act(async () => { fireEvent.keyDown(ta, { key: 'Backspace' }) })

      // The mention and its chip are untouched -- the optimizer owns the draft.
      expect(ta.value).toContain('@src/main.ts')
      expect(screen.getByLabelText('Remove')).toBeInTheDocument()
    } finally {
      await act(async () => { release(null) })
      vi.stubGlobal('fetch', realFetch)
    }
  })
})
