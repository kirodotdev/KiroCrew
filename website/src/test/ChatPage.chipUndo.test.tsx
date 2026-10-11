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

function makeStore(
  activeSlot: string,
  slots: { key: string; project?: string }[],
  messages: { role: string; content: string; cls: string }[] = [{ role: 'assistant', content: 'hi', cls: '' }],
) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slots: slots.map(s => ({ key: s.key, project: s.project, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages,
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

  it('a follow-up chip Send-now sends the staged file AND clears it so the next turn does not re-send it', async () => {
    // The follow-up chip's immediate send routes through send(optionText), which
    // skips the normal composer clear to protect the user's typed DRAFT -- but the
    // staged attachments ARE consumed and transmitted on that path, so leaving
    // them staged re-attached the same media to the next human turn. Stage a
    // file, type some plain draft text, fire the chip's "Send now", then assert
    // the file went out once, the attachment chip is gone, AND the typed draft
    // the !optionText gate exists to protect is still in the composer.
    const store = makeStore(
      'slot-a',
      [{ key: 'slot-a', project: '/repo' }],
      [{ role: 'user', content: 'go', cls: '' }, { role: 'assistant', content: 'Pick one [OPTIONS: Alpha | Beta]', cls: '' }],
    )
    await renderPage(store)
    const ta = await pickFile()
    // Plain text the user is still composing; it must survive the chip send.
    fireEvent.change(ta, { target: { value: ta.value + 'keep me' } })

    // The chip's Send-now segment (not a plain chip click, which only appends to
    // the composer). aria-label carries the option text -- target Alpha's.
    const sendNow = await screen.findByRole('button', { name: /send now.*Alpha/i })
    await act(async () => { fireEvent.click(sendNow) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    const sent = vi.mocked(api.sendChat).mock.calls.at(-1)![0] as string
    // The option text and the staged file both went out in this one send.
    expect(sent).toContain('Alpha')
    expect(sent).toContain('[attached_file 1] /repo/src/main.ts')

    // The attachment is cleared: its chip is gone, so a subsequent human send
    // cannot silently re-attach it.
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    // ...but the user's typed draft the gate protects is untouched.
    expect(ta.value).toContain('keep me')
  })
  // #18263: the keydown atomic delete covers Backspace/Delete only. A staged
  // mention erased through the textarea's VALUE-CHANGE seam -- a type-over, a
  // cut, or a paste-over, where a selection overlapping the mention is replaced
  // in one `change` event -- is not a keydown. The seam handles it STRUCTURALLY
  // in a native `beforeinput` listener: it reads the pre-edit selection, and if
  // that selection cuts into a staged mention it expands the replaced range to
  // the whole mention, preventDefault's the browser's partial edit, and applies
  // the whole replacement through the controlled value. No after-the-fact diff.

  // Drive one real selection-replace edit the way the browser delivers it: set
  // the selection [selStart, selEnd), then dispatch a native `beforeinput`
  // InputEvent carrying the edit's own inputType and inserted `data`. The
  // component (not the test) computes the resulting value, exactly as in a
  // browser. jsdom supports InputEvent.inputType/data; it has no fireEvent
  // helper for beforeinput, so dispatch the native event directly.
  function seamEdit(ta: HTMLTextAreaElement, selStart: number, selEnd: number, inputType: string, data: string | null) {
    ta.setSelectionRange(selStart, selEnd)
    ta.dispatchEvent(new (window as unknown as { InputEvent: typeof InputEvent }).InputEvent('beforeinput', {
      bubbles: true, cancelable: true, inputType, data: data ?? undefined,
    }))
  }

  // Type-over: select the mention's tail and type `x` over it. The seam expands
  // to the whole mention and replaces it with `x`, so no `@src/` half-reference
  // survives and the chip unstages. (On origin/main the native edit left
  // `@src/x` and silently unstaged the chip.)
  it('typing over a selection that overlaps a staged mention removes the whole mention (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    seamEdit(ta, selStart, selEnd, 'insertText', 'x')

    await waitFor(() => expect(ta.value).toBe('please review x for the bug'))
    expect(ta.value).not.toContain('@src/')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    const llm = await send(ta)
    expect(llm).not.toMatch(/\[attached_file \d\]/)
    expect(llm).not.toContain('@src/')
  })

  // Cut (deleteByCut): a selection overlapping the mention is cut. The seam
  // expands to the whole mention and removes it (empty inserted data); the
  // clipboard is written by the browser's own cut, which this does not touch.
  it('cutting a selection that overlaps a staged mention removes the whole mention (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts for'.length
    seamEdit(ta, selStart, selEnd, 'deleteByCut', null)

    // Whole mention gone; `for` (inside the selection) gone too; no `@src/`.
    await waitFor(() => expect(ta.value).toBe('please review  the bug'))
    expect(ta.value).not.toContain('@src/')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    const llm = await send(ta)
    expect(llm).not.toMatch(/\[attached_file \d\]/)
  })

  // Paste-over (insertFromPaste): paste text over a selection overlapping the
  // mention. The seam expands to the whole mention and replaces it with the
  // pasted text; no `@src/` fragment survives.
  it('pasting over a selection that overlaps a staged mention removes the whole mention (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    seamEdit(ta, selStart, selEnd, 'insertFromPaste', 'README.md')

    await waitFor(() => expect(ta.value).toBe('please review README.md for the bug'))
    expect(ta.value).not.toContain('@src/')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    const llm = await send(ta)
    expect(llm).not.toMatch(/\[attached_file \d\]/)
  })

  // Pasting text that STARTS with the mention's own characters keeps the whole
  // paste: the inserted data comes straight from the event, never inferred from
  // a text diff that would read the shared `main.` as unchanged and drop it.
  it('pasting text sharing the mention\'s prefix keeps the whole paste (#18263, no data loss)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    seamEdit(ta, selStart, selEnd, 'insertFromPaste', 'main.py')

    await waitFor(() => expect(ta.value).toBe('please review main.py for the bug'))
    expect(ta.value).not.toContain('@src/')
    expect(ta.value).toContain('main.py')
  })

  // Selecting the ENTIRE mention and typing `@`: the selection fully COVERS the
  // mention (its boundaries are not strictly inside), so the seam expands
  // nothing and the browser's own edit runs. The chip reconciliation then
  // unstages the chip because `@` is not a staged alias. No data is lost.
  it('typing @ over a fully selected mention keeps the typed @ and unstages (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    // Fully covering selection -> seam no-ops -> native edit; model the native result.
    ta.setSelectionRange('please review '.length, 'please review @src/main.ts'.length)
    ta.dispatchEvent(new (window as unknown as { InputEvent: typeof InputEvent }).InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: '@' }))
    fireEvent.change(ta, { target: { value: 'please review @ for the bug', selectionStart: 'please review @'.length, selectionEnd: 'please review @'.length } })

    expect(ta.value).toBe('please review @ for the bug')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // While an IME composition is live, the seam must stay hands-off: an
  // insertCompositionText beforeinput (or isComposing) is left to the browser,
  // so a CJK type-over over a mention is not corrupted mid-composition.
  it('a composition edit over a staged mention does not corrupt the composed text (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    ta.setSelectionRange(selStart, selEnd)
    fireEvent.compositionStart(ta)
    // insertCompositionText beforeinput: the seam ignores it (no preventDefault).
    const ev = new (window as unknown as { InputEvent: typeof InputEvent }).InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertCompositionText', data: '\u3042', isComposing: true })
    ta.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(false)
    // The browser then applies the composition; model that native result.
    fireEvent.change(ta, { target: { value: 'please review @src/\u3042 for the bug', selectionStart: 'please review @src/\u3042'.length, selectionEnd: 'please review @src/\u3042'.length } })
    expect(ta.value).toBe('please review @src/\u3042 for the bug')
  })

  // An edit that does NOT touch the mention leaves it (and its chip) alone --
  // the seam only acts on a selection that cuts into a staged mention.
  it('an edit elsewhere in the text leaves an untouched staged mention and its chip (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    // Type-over `bug` (far from the mention) with `issue`: seam no-ops, native edit.
    ta.setSelectionRange('please review @src/main.ts for the '.length, 'please review @src/main.ts for the bug'.length)
    ta.dispatchEvent(new (window as unknown as { InputEvent: typeof InputEvent }).InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: 'issue' }))
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the issue', selectionStart: 40, selectionEnd: 40 } })

    expect(ta.value).toContain('@src/main.ts')
    expect(screen.getByLabelText('Remove')).toBeInTheDocument()
    const llm = await send(ta)
    expect(llm).toContain('[attached_file 1] /repo/src/main.ts')
  })

  // A cut whose selection fully COVERS the mention (plus surrounding text) is a
  // clean whole removal: the boundaries are not strictly inside the mention, so
  // the seam expands nothing and the browser's own delete runs. The chip
  // unstages because the mention is gone.
  it('cutting a selection that fully covers a staged mention removes it cleanly (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    // Selection `review @src/main.ts for` fully covers the mention (starts
    // before the `@`, ends after the mention) -> seam no-ops, native cut.
    ta.setSelectionRange('please '.length, 'please review @src/main.ts for'.length)
    ta.dispatchEvent(new (window as unknown as { InputEvent: typeof InputEvent }).InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'deleteByCut', data: null as unknown as string }))
    fireEvent.change(ta, { target: { value: 'please  the bug', selectionStart: 'please '.length, selectionEnd: 'please '.length } })

    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
    expect(ta.value).toBe('please  the bug')
    const llm = await send(ta)
    expect(llm).not.toMatch(/\[attached_file \d\]/)
  })

  // Left-boundary rebuild: select `review @` (so the selection's end is strictly
  // inside the mention, cutting it) and type `@`. The seam expands to the whole
  // mention and replaces it with the typed `@`, so the unselected `src/main.ts`
  // tail cannot survive as a half-reference. (A per-span survival diff missed
  // this; the structural range-expansion handles it directly.)
  it('typing @ over a selection whose end cuts into the mention removes it whole (#18263, left-boundary rebuild)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please '.length // start of `review`
    const selEnd = 'please review @'.length // just past the mention's `@` (strictly inside)
    seamEdit(ta, selStart, selEnd, 'insertText', '@')

    // Whole mention removed; the typed `@` kept; no `src/main.ts` fragment.
    await waitFor(() => expect(ta.value).toBe('please @ for the bug'))
    expect(ta.value).not.toContain('src/main.ts')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // Drop (insertFromDrop): dropping text over a selection overlapping the
  // mention expands to the whole mention and replaces it with the dropped text.
  it('dropping text over a selection that overlaps a staged mention removes the whole mention (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    seamEdit(ta, selStart, selEnd, 'insertFromDrop', 'dropped')

    await waitFor(() => expect(ta.value).toBe('please review dropped for the bug'))
    expect(ta.value).not.toContain('@src/')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // insertReplacementText (autocorrect / spellcheck replace) over a selection
  // overlapping the mention is handled the same way.
  it('an autocorrect replacement over a staged mention removes the whole mention (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    seamEdit(ta, selStart, selEnd, 'insertReplacementText', 'fixed')

    await waitFor(() => expect(ta.value).toBe('please review fixed for the bug'))
    expect(ta.value).not.toContain('@src/')
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  // A keystroke that lands right after a seam edit is not clobbered: the seam
  // applies through the controlled value and restores the caret only while the
  // live value still matches, so a newer edit always wins.
  it('a keystroke right after a seam edit is not clobbered (#18263, no data loss)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    seamEdit(ta, selStart, selEnd, 'insertText', 'x')
    await waitFor(() => expect(ta.value).toBe('please review x for the bug'))
    // A second character lands before the seam's caret-restore frame runs.
    const caret = 'please review x'.length
    fireEvent.change(ta, { target: { value: 'please review xy for the bug', selectionStart: caret + 1, selectionEnd: caret + 1 } })
    await new Promise(r => requestAnimationFrame(() => r(null)))
    expect(ta.value).toBe('please review xy for the bug')
  })

  // When the SAME mention is staged twice, a selection that cuts into the first
  // occurrence while the edit rebuilds a complete mention must NOT delete the
  // other, still-intact occurrence. Structurally the seam only expands and
  // replaces the range that actually cuts a mention; the unselected second
  // occurrence is outside the replaced range and is preserved.
  it('a cut into one of two identical mentions keeps the other intact occurrence (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: '@src/main.ts @src/main.ts' } })
    await waitFor(() => expect(ta.value).toBe('@src/main.ts @src/main.ts'))
    await screen.findByLabelText('Remove')
    // Select from strictly inside the FIRST mention to the end of the SECOND and
    // paste `main.ts @src/main.ts` back -> the net text is unchanged, but the
    // first mention was cut, so the seam expands [5, end) to [0, end) and
    // replaces with the pasted data, rebuilding both occurrences intact.
    const selStart = '@src/'.length
    const selEnd = '@src/main.ts @src/main.ts'.length
    seamEdit(ta, selStart, selEnd, 'insertFromPaste', 'main.ts @src/main.ts')

    await waitFor(() => expect(ta.value).toBe('@src/main.ts @src/main.ts'))
    expect(screen.getByLabelText('Remove')).toBeInTheDocument()
  })

  // FAIL-SAFE (Design review): an INSERT beforeinput whose engine put the
  // inserted text on neither `data` nor `dataTransfer` must NOT be claimed --
  // deleting the mention and inserting nothing would be WORSE than the
  // half-reference this fixes (silent text loss). The seam leaves the event
  // un-prevented so the browser applies its own edit; the mention is reconciled
  // on a later edit that does carry its data. A delete legitimately carries no
  // data and is still claimed (its own test above covers that).
  it('an insert beforeinput with no data over a mention is left to the browser (#18263, fail-safe)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    ta.setSelectionRange(selStart, selEnd)
    // insertText with null data and no dataTransfer: the seam must not claim it.
    const ev = new (window as unknown as { InputEvent: typeof InputEvent }).InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: null })
    ta.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(false)
    // The mention text is untouched (the seam did not rewrite it to nothing).
    expect(ta.value).toContain('@src/main.ts')
  })

  // A COLLAPSED-caret insertion inside a mention (no selection) is a plain edit
  // the user is making, not a selection-replace: typing a character at
  // `@src/|main.ts` must insert it, never expand the empty range to the whole
  // mention and delete it (GPT 6.1 review). The seam leaves a non-delete edit
  // with selectionStart === selectionEnd to the browser.
  it('typing a character with the caret INSIDE a mention (no selection) is left to the browser (#18263)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    // Collapsed caret strictly inside the mention (`@src/|main.ts`).
    const caret = 'please review @src/'.length
    ta.setSelectionRange(caret, caret)
    const ev = new (window as unknown as { InputEvent: typeof InputEvent }).InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: 'x' })
    ta.dispatchEvent(ev)
    // The seam must NOT claim it: the browser applies its own one-char insert.
    expect(ev.defaultPrevented).toBe(false)
    // The mention is not nuked to `@src/x`; its text is still fully present.
    expect(ta.value).toContain('@src/main.ts')
  })

  // A NON-CANCELABLE beforeinput cannot be stopped: on Chromium Android an IME
  // edit over a mention (e.g. a Backspace delivered as a non-cancelable
  // `deleteContentBackward`) arrives this way, so preventDefault is a no-op and
  // the browser would apply its own partial edit on top of the seam's rewrite,
  // corrupting the draft. The seam stands down -- it neither prevents the event
  // nor rewrites the value -- and lets the browser own the edit (Opus 5.5
  // review). The list-continuation sibling guards the same case.
  it('a non-cancelable beforeinput over a mention is left to the browser (#18263, Chromium Android)', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await pickFile()
    fireEvent.change(ta, { target: { value: 'please review @src/main.ts for the bug' } })
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    const selStart = 'please review @src/'.length
    const selEnd = 'please review @src/main.ts'.length
    ta.setSelectionRange(selStart, selEnd)
    // cancelable: false -- the one bit that distinguishes the Android IME path.
    const ev = new (window as unknown as { InputEvent: typeof InputEvent }).InputEvent('beforeinput', { bubbles: true, cancelable: false, inputType: 'deleteContentBackward', data: null })
    ta.dispatchEvent(ev)
    // The seam did not call preventDefault (it would be a no-op anyway) and did
    // not rewrite the controlled value -- the mention text is still fully there.
    expect(ta.value).toContain('@src/main.ts')
  })
})

/** Render the page, let `prepare` shape the draft and return the textarea,
 *  click Optimize prompt with the optimizer answering `optimized`, and wait
 *  for the composer to settle. */
async function optimizePageWith(optimized: string, prepare: () => Promise<HTMLTextAreaElement>) {
  let calls = 0
  const realFetch = globalThis.fetch
  vi.stubGlobal('fetch', (url: string) => {
    if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
      calls += 1
      return Promise.resolve({ ok: true, json: async () => ({ changed: true, optimized }) })
    }
    return Promise.resolve({ ok: true, json: async () => [] } as unknown as Response)
  })
  try {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)
    const ta = await prepare()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' })) })
    await waitFor(() => expect(calls).toBe(1))
    await waitFor(() => expect(ta).not.toHaveAttribute('readonly'))
    return ta
  } finally {
    vi.stubGlobal('fetch', realFetch)
  }
}

// #13063: the prompt optimizer's rewrite lands in the draft as a whole-text
// replacement, and the chip reconciliation then reads it like any other edit.
// A rewrite that drops or rewords a picked `@rel` mention would unstage that
// file's chip while the composer was readOnly -- no user edit -- and the send
// would go out without the attachment. A rewrite is kept only when the
// reconciliation would leave the attachments as the draft does.
describe('ChatPage prompt optimizer keeps picked file mentions (#13063)', { timeout: 15_000 }, () => {
  const DRAFT = 'please review @src/main.ts for the bug'

  /** Pick main.ts, let `edit` shape the draft, click Optimize prompt with the
   *  optimizer answering `optimized`, and wait for the composer to settle. */
  const optimizeWith = (optimized: string, edit = async (ta: HTMLTextAreaElement) => {
    fireEvent.change(ta, { target: { value: DRAFT } })
    await waitFor(() => expect(ta.value).toBe(DRAFT))
  }) => optimizePageWith(optimized, async () => {
    const ta = await pickFile()
    await edit(ta)
    return ta
  })

  it('a rewrite that drops the mention leaves the draft and its chip alone', async () => {
    const ta = await optimizeWith('Review the main entry file and fix the bug.')
    expect(ta.value).toBe(DRAFT)
    expect(screen.getByLabelText('Remove')).toBeInTheDocument()
  })

  it('a rewrite that keeps the text but breaks the mention boundary is refused too', async () => {
    // `@src/main.ts` is still a substring, but `'s` is not a boundary the
    // reconciliation accepts, so it would read the mention as gone.
    const ta = await optimizeWith("Fix @src/main.ts's bug.")
    expect(ta.value).toBe(DRAFT)
    expect(screen.getByLabelText('Remove')).toBeInTheDocument()
  })

  it('a rewrite that keeps the mention is applied and the chip stays staged', async () => {
    const ta = await optimizeWith('Review @src/main.ts and fix the bug.')
    await waitFor(() => expect(ta.value).toBe('Review @src/main.ts and fix the bug.'))
    expect(screen.getByLabelText('Remove')).toBeInTheDocument()
  })

  it('a rewrite that brings back a removed chip\'s mention is refused', async () => {
    // The chip's ✕ keeps its aliases so an undo can restore it; a rewrite that
    // re-adds the mention would restage the file the user just removed.
    const ta = await optimizeWith('Review @src/main.ts for the bug.', async (el) => {
      fireEvent.click(screen.getByLabelText('Remove'))
      await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
      fireEvent.change(el, { target: { value: 'please review the bug' } })
      await waitFor(() => expect(el.value).toBe('please review the bug'))
    })
    expect(ta.value).toBe('please review the bug')
    expect(screen.queryByLabelText('Remove')).toBeNull()
  })
})

// A folder chip is derived from its `@rel/` token in the draft, so a rewrite
// that rewords, drops or adds a folder token would change the folder chips
// while the composer was readOnly -- no user edit -- and the send would go out
// with a different set of folders. A rewrite is kept only when its folder
// tokens are exactly the draft's.
describe('ChatPage prompt optimizer keeps folder mentions', { timeout: 15_000 }, () => {
  const DRAFT = 'please review @src/utils/ for the bug'

  /** Type DRAFT, click Optimize prompt with the optimizer answering
   *  `optimized`, and wait for the composer to settle. */
  const optimizeWith = (optimized: string) => optimizePageWith(optimized, async () => {
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: DRAFT } })
    await waitFor(() => expect(ta.value).toBe(DRAFT))
    await screen.findByLabelText('Remove folder')
    return ta
  })

  it('a rewrite that turns the folder mention into prose leaves the draft and its chip alone', async () => {
    const ta = await optimizeWith('Review the utils folder and fix the bug.')
    expect(ta.value).toBe(DRAFT)
    expect(screen.getByLabelText('Remove folder')).toBeInTheDocument()
  })

  it('a rewrite that keeps the text but breaks the folder token boundary is refused too', async () => {
    // `@src/utils/` is still a substring, but a folder token must end at
    // whitespace or the end of the text, so `.` would drop the chip.
    const ta = await optimizeWith('Fix the bug in @src/utils/.')
    expect(ta.value).toBe(DRAFT)
    expect(screen.getByLabelText('Remove folder')).toBeInTheDocument()
  })

  it('a rewrite that adds a folder mention is refused', async () => {
    const ta = await optimizeWith('Review @src/utils/ and @src/lib/ for the bug.')
    expect(ta.value).toBe(DRAFT)
    expect(screen.getAllByLabelText('Remove folder')).toHaveLength(1)
  })

  it('a rewrite that keeps the folder mention is applied and the chip stays', async () => {
    const ta = await optimizeWith('Review @src/utils/ and fix the bug.')
    await waitFor(() => expect(ta.value).toBe('Review @src/utils/ and fix the bug.'))
    expect(screen.getByLabelText('Remove folder')).toBeInTheDocument()
  })
})
