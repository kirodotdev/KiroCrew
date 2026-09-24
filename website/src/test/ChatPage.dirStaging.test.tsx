import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, openActivityPanel } from '../store/chatSlice'
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
    // `root` echoes the QUERIED project (falling back to '/repo' when a test's
    // slot carries none) -- FilePickerMenu relativizes against the search
    // response's own `root`, not the `project` prop directly, so a mock that
    // ignored the query would give every slot the same root regardless of its
    // actual project.
    fileSearch: vi.fn().mockImplementation((_q: string, project?: string) => Promise.resolve({
      root: project || '/repo',
      results: [
        { path: '/repo/src/widgets', name: 'widgets', size: 0, mtime: Math.floor(Date.now() / 1000) - 60, kind: 'dir' },
        { path: '/repo/src/main.ts', name: 'main.ts', size: 10, mtime: Math.floor(Date.now() / 1000) - 60, kind: 'file' },
      ],
    })),
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
// A minimal stand-in for the real SidePanel, exposing just enough to drive
// handleAddToContext's FILE branch the way the tree's "Add to chat" context
// menu action does -- the only entry point that call reaches (the picker
// goes through ChatInput's own onFileSelect instead, a different path).
vi.mock('../pages/chat/SidePanel', () => ({
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => undefined,
  default: ({ onAddToContext }: { onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void }) => (
    // Each stub control lives in its own wrapper, one per row -- not a
    // horizontal sibling group of 4 action buttons (fork GPT review,
    // AUTOSDE `max-two-buttons-per-row`). These are individually-triggered
    // test fixtures, not a real action row.
    <>
      <div><button onClick={() => onAddToContext?.('/repo/src/main.ts', 'file')}>Add to chat: main.ts</button></div>
      {/* On POSIX this is a single literal filename, not a nested src/main.ts
          path. Its suffix behavior must never borrow Windows separator rules. */}
      <div><button onClick={() => onAddToContext?.('/repo/src\\main.ts', 'file')}>Add to chat: src\\main.ts (literal POSIX)</button></div>
      {/* A second, DIFFERENT absolute file that happens to share the trailing
          `src/main.ts` path segment -- for the suffix-collision regression
          test only. */}
      <div><button onClick={() => onAddToContext?.('/repo/other/src/main.ts', 'file')}>Add to chat: other/src/main.ts</button></div>
      {/* Forward-slash-normalized (the tree always provides this form), for a
          genuinely Windows-shaped project (`C:\repo`) -- the backslash-form
          "already mentioned" test needs a project where separator folding is
          actually valid, not merely a POSIX-looking project string. */}
      <div><button onClick={() => onAddToContext?.('C:/repo/src/main.ts', 'file')}>Add to chat: main.ts (win project)</button></div>
      {/* A second, DIFFERENT absolute file under a Windows-shaped project --
          for the round-14 shared-alias-plus-separator-edit regression test. */}
      <div><button onClick={() => onAddToContext?.('C:/repo/other/src/main.ts', 'file')}>Add to chat: other/src/main.ts (win project)</button></div>
      {/* A file whose rel is the EXACT prefix of another staged file's rel,
          differing only by a trailing comma that is a literal, legal
          filename character -- for the round-18 prefix/punctuation
          collision regression test. */}
      <div><button onClick={() => onAddToContext?.('/repo/report', 'file')}>Add to chat: report</button></div>
      <div><button onClick={() => onAddToContext?.('/repo/report,', 'file')}>Add to chat: report,</button></div>
      {/* The same prefix/punctuation sibling pair under a WINDOWS-shaped
          project -- for the mixed-separator revival guard test (fork GPT
          review): the sibling set must fold both separator spellings or a
          flipped-spelling paste slips past the prefix rule. */}
      <div><button onClick={() => onAddToContext?.('C:/repo/win/report', 'file')}>Add to chat: win/report (win project)</button></div>
      <div><button onClick={() => onAddToContext?.('C:/repo/win/report,', 'file')}>Add to chat: win/report, (win project)</button></div>
      {/* A sibling pair where the LONGER file's rel extends the shorter's
          with `:42` (`:` is a legal POSIX filename character) -- the case
          that forces strictMentionBoundary and gates `:line`-suffix
          consumption OFF in the remove-chip strip (fork Opus review). */}
      <div><button onClick={() => onAddToContext?.('/repo/a.ts', 'file')}>Add to chat: a.ts</button></div>
      <div><button onClick={() => onAddToContext?.('/repo/a.ts:42', 'file')}>Add to chat: a.ts:42</button></div>
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

  it('hand-editing a picked file token out of the composer drops the orphaned chip', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: '@mai' } })
    const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(row)
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    // Deleting the token by hand (no chip-remove click) must unstage the
    // file too -- the same "text is the source of truth" contract a folder
    // token already gets for free (see the orphaned-folder-chip test above).
    fireEvent.change(ta, { target: { value: 'no file here anymore' } })

    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  it('a token cut then pasted back (move, or an undo) restages the file chip', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: '@mai' } })
    const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(row)
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    // Cut the token out -- same as the orphan test, the chip unstages.
    fireEvent.change(ta, { target: { value: 'move it down here: ' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())

    // Paste the SAME token back elsewhere in the text (or an undo restoring
    // it) -- the file was never really un-referenced, so its chip must come
    // back too, the same way a folder chip already survives this round trip.
    fireEvent.change(ta, { target: { value: 'move it down here: @src/main.ts' } })
    await screen.findByLabelText('Remove')
  })

  it('the same file picked under two differently-rooted slots keeps both chips (pickedFileTokens is not slot-scoped)', async () => {
    // Both slots can stage the SAME absolute path (/repo/src/main.ts) --
    // slot-a's project is the repo root, slot-b's is the src/ subdirectory,
    // so the SAME file relativizes to two different @rel forms per slot.
    // pickedFileTokens is a single map keyed by absolute path shared across
    // slots, so the second pick overwrites the first slot's recorded token
    // string; the reconciliation effect must not trust that stored string,
    // or switching back to slot-a silently drops its still-valid chip.
    const store = makeStore('slot-a', [
      { key: 'slot-a', project: '/repo' },
      { key: 'slot-b', project: '/repo/src' },
    ])
    await renderPage(store)

    const taA = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(taA, { target: { value: '@mai' } })
    const rowA = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(rowA)
    await waitFor(() => expect(taA.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())

    const taB = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(taB, { target: { value: '@mai' } })
    // The picker's `placeholderData` keeps slot-a's LAST resolved results on
    // screen while slot-b's own (differently-rooted) query is in flight, so
    // "main.ts" can satisfy findByText from the stale placeholder alone.
    // Wait for the query to actually have been ISSUED with slot-b's project
    // before trusting the row -- otherwise a click can land while the menu is
    // still showing slot-a's root and relativize against the wrong one.
    await waitFor(() => expect(vi.mocked(api.fileSearch).mock.calls.some(c => c[1] === '/repo/src')).toBe(true))
    const rowB = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(rowB)
    // Under slot-b's project the SAME absolute file relativizes to a bare
    // `@main.ts`, overwriting pickedFileTokens' shared entry for this path.
    await waitFor(() => expect(taB.value).toContain('@main.ts'))
    await screen.findByLabelText('Remove')

    // Back to slot-a: its own text/chip must survive the foreign overwrite.
    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    await screen.findByLabelText('Remove')
    const taA2 = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(taA2.value).toContain('@src/main.ts')
  })

  it('sending in one slot does not blank another slot\'s recorded file token (pickedFileTokens is not wiped wholesale)', async () => {
    // pickedFileTokens is a single map shared across every slot. Send used to
    // clear it wholesale (`pickedFileTokens.current = {}`), so sending in
    // slot-b would silently drop the token bookkeeping for a file staged in
    // slot-a, even though slot-a's own attachment was never touched (fork
    // GPT review). Losing that bookkeeping would make the reconciliation
    // effect blind to a later hand-edit on slot-a's chip, recreating the
    // exact orphaned-chip bug this PR fixes.
    const store = makeStore('slot-a', [
      { key: 'slot-a', project: '/repo' },
      { key: 'slot-b', project: '/repo' },
    ])
    await renderPage(store)

    const taA = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(taA, { target: { value: '@mai' } })
    const rowA = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(rowA)
    await waitFor(() => expect(taA.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())

    const taB = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(taB, { target: { value: 'unrelated message' } })
    await act(async () => { fireEvent.keyDown(taB, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())

    // Back to slot-a: its token bookkeeping must have survived slot-b's send,
    // so hand-deleting the token still unstages the chip -- if the send had
    // wiped it wholesale, the chip would be stuck with no recorded token to
    // reconcile against (the orphaned-chip bug, reintroduced via a side door).
    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    await screen.findByLabelText('Remove')
    const taA2 = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(taA2.value).toContain('@src/main.ts')

    fireEvent.change(taA2, { target: { value: 'no file here anymore' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  it('the same file staged in two slots keeps slot-a reconcilable after slot-b sends it', async () => {
    // Both slots stage the SAME absolute file (same project, so the same
    // rel too) -- pickedFileTokens is keyed by absolute path only, so this
    // is one shared entry. Sending slot-b's copy must not blank that shared
    // entry out from under slot-a's still-staged copy (fork GPT review,
    // round 2): if it did, slot-a's chip would survive but the reconciliation
    // effect would no longer recognize it, so a later hand-edit could never
    // unstage it -- an orphaned chip reintroduced through a second side door.
    const store = makeStore('slot-a', [
      { key: 'slot-a', project: '/repo' },
      { key: 'slot-b', project: '/repo' },
    ])
    await renderPage(store)

    const taA = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(taA, { target: { value: '@mai' } })
    const rowA = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(rowA)
    await waitFor(() => expect(taA.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())

    const taB = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(taB, { target: { value: '@mai' } })
    const rowB = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(rowB)
    await waitFor(() => expect(taB.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    // Slot-b sends its own copy of the same file.
    await act(async () => { fireEvent.keyDown(taB, { key: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())

    // Slot-a's copy must still be staged AND still reconcilable: a hand-edit
    // on its token should still unstage the chip, proving the shared entry
    // survived slot-b's send instead of being silently deleted out from
    // under it.
    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    await screen.findByLabelText('Remove')
    const taA2 = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(taA2.value).toContain('@src/main.ts')

    fireEvent.change(taA2, { target: { value: 'no file here anymore' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  it('changing the slot\'s project after picking a file does not unstage it on the next edit', async () => {
    // The reconciliation effect compares the STORED token, not a rel
    // re-derived against the slot's CURRENT project (fork GPT review, round
    // 3): the composer text was never rewritten when the project changed, so
    // the token sitting in it is still the OLD, correct one -- re-deriving
    // against the new project would falsely call it stale and drop a file
    // the user never touched.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: '@mai' } })
    const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(row)
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    // Change the slot's project WITHOUT switching slots -- e.g. the user
    // repoints this same chat at a different folder.
    act(() => { store.dispatch(updateSlot({ key: 'slot-a', project: '/elsewhere' })) })

    // An unrelated composer edit re-runs the reconciliation effect; it must
    // not touch the still-valid, untouched file token.
    fireEvent.change(ta, { target: { value: ta.value + ' please' } })
    await screen.findByLabelText('Remove')
    expect(ta.value).toContain('@src/main.ts')
  })

  it('"Add to chat" on an already-mentioned file still records its token, so a later hand-edit unstages it', async () => {
    // handleAddToContext's file branch only recorded the token INSIDE its
    // `!alreadyMentioned` branch -- if the text already had the mention (the
    // tree's "Add to chat" on a file the user already typed an `@` reference
    // to), the chip still staged (setPendingFiles runs unconditionally) but
    // no bookkeeping was ever written, so hand-deleting the mention later
    // left an orphaned chip with no recorded token to reconcile against
    // (fork GPT review) -- the exact bug this PR fixes, reached a different
    // way.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'please check @src/main.ts' } })

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('please check @src/main.ts')

    fireEvent.change(ta, { target: { value: 'no file here anymore' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  it('revival never resurrects a stale entry whose rel now belongs to a different project', async () => {
    // The stored token alone is not enough to trust a coincidental rel match
    // for REVIVAL (fork GPT review): if the file was unstaged, the slot's
    // project then changed, and the SAME rel text later reappears -- typed
    // with the NEW project's own file in mind -- reviving the OLD absolute
    // path would silently attach the wrong file.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: '@mai' } })
    const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(row)
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    fireEvent.change(ta, { target: { value: 'no file here anymore' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())

    act(() => { store.dispatch(updateSlot({ key: 'slot-a', project: '/elsewhere' })) })

    fireEvent.change(ta, { target: { value: 'now referencing @src/main.ts' } })
    // Give the reconciliation effect a beat to (not) act, then assert no
    // chip was resurrected for the stale, wrong-project absolute path.
    await new Promise(r => setTimeout(r, 50))
    expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument()
  })

  it('"Add to chat" on a backslash-form existing mention still records a token on a Windows-shaped project (fork GPT review)', async () => {
    // Same gap as the forward-slash "already-mentioned" test above, but for
    // the separator rendition the Windows @-picker itself inserts:
    // buildRelMap's suffix walk only ever tries forward-slash, so it silently
    // found nothing for a `@src\main.ts` mention and left this file with no
    // recorded token at all. Requires a genuinely Windows-shaped project --
    // a POSIX project must NOT fold the two separators (round-10 regression
    // test below covers that case).
    const store = makeStore('slot-a', [{ key: 'slot-a', project: 'C:\\repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'please check @src\\main.ts' } })

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts (win project)'))
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('please check @src\\main.ts')

    fireEvent.change(ta, { target: { value: 'no file here anymore' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).not.toBeInTheDocument())
  })

  it('"Add to chat" on POSIX treats a literal backslash mention as a different file', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'please check @src\\main.ts' } })

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('please check @src\\main.ts @src/main.ts ')
  })

  it('a second alias for the same file (after a project change) survives deleting the other one', async () => {
    // pickedFileTokens used to store ONE token string per path -- a second
    // pick of the SAME absolute file, under a project that gives it a
    // DIFFERENT rel, overwrote the first alias outright even though both
    // texts coexist in the composer. Deleting the (now sole-recorded) second
    // alias then unstaged the file despite the first alias still sitting
    // right there, untouched (fork GPT review).
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: '@mai' } })
    const row = await screen.findByText('main.ts', undefined, { timeout: 3000 })
    fireEvent.mouseDown(row)
    await waitFor(() => expect(ta.value).toContain('@src/main.ts'))
    await screen.findByLabelText('Remove')

    // Same file, now relative to a project one level down -- a fresh pick
    // computes a DIFFERENT rel for the identical absolute path.
    act(() => { store.dispatch(updateSlot({ key: 'slot-a', project: '/repo/src' })) })
    fireEvent.change(ta, { target: { value: ta.value + ' and @mai' } })
    // As in the cross-slot test above: the picker's placeholderData keeps
    // the FIRST query's (project '/repo') results on screen while the
    // second, differently-rooted query is in flight. Waiting merely for the
    // call to have been ISSUED is not enough -- the row's rendered NAME text
    // is project-independent (only its computed relativePath differs), so a
    // retry loop keyed on row presence can't tell placeholder from real data
    // apart. Wait for the call's own promise to settle instead.
    await waitFor(() => expect(vi.mocked(api.fileSearch).mock.calls.some(c => c[1] === '/repo/src')).toBe(true))
    await vi.mocked(api.fileSearch).mock.results.at(-1)?.value
    await new Promise(r => setTimeout(r, 0))
    // The file is already staged (its chip shows "main.ts" too), so the
    // dropdown's OWN "main.ts" row is now ambiguous against the chip's text
    // -- scope to an actual [role="option"] row, re-queried on every retry
    // since the placeholder-vs-real query swap can replace the row element
    // between an existence check and a later click on a stale reference.
    await waitFor(() => {
      const opt = [...document.querySelectorAll('[role="option"]')].find(el => el.textContent?.includes('main.ts'))
      if (!opt) throw new Error('main.ts option row not found yet')
      fireEvent.mouseDown(opt)
    }, { timeout: 3000 })
    await waitFor(() => expect(ta.value).toContain('@main.ts'))
    expect(ta.value).toContain('@src/main.ts')

    // Delete only the SECOND alias -- the first is still right there.
    fireEvent.change(ta, { target: { value: 'please check @src/main.ts and ' } })
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('please check @src/main.ts and ')
  })

  it('deleting one staged file\'s mention does not keep it staged just because a DIFFERENT staged file shares a path suffix', async () => {
    // A boundary-checked path-SUFFIX fallback for "is this file still
    // mentioned" (tried and reverted -- fork GPT review) is unsafe once more
    // than one staged file can share a trailing path segment:
    // /repo/src/main.ts and /repo/other/src/main.ts both end in
    // `src/main.ts`. Only each file's own EXACTLY recorded alias(es) may
    // prove it is still referenced -- never a suffix borrowed from a
    // sibling file's own, unrelated mention.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    fireEvent.click(await screen.findByText('Add to chat: other/src/main.ts'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/main.ts')
    expect(ta.value).toContain('@other/src/main.ts')

    // Delete ONLY the second file's mention -- the first's own `@src/main.ts`
    // remains, and happens to be a trailing suffix of the second file's path.
    fireEvent.change(ta, { target: { value: 'please check @src/main.ts ' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    expect(ta.value).toBe('please check @src/main.ts ')
  })

  it('deleting a staged file\'s mention does not survive via an unrelated literal-backslash mention on a POSIX project', async () => {
    // `\` is a legal filename character on POSIX, so a genuinely typed
    // `@src\main.ts` (a plain-text reference someone typed, not this file's
    // own alias) must NOT be read as proof that `@src/main.ts` is "still
    // mentioned" -- that folding is only sound on a Windows-shaped project,
    // where the OS itself treats the two separators as interchangeable
    // (fork GPT review).
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/main.ts')

    // Delete the file's own mention, replacing it with an unrelated literal
    // backslash mention that merely happens to fold to the same string.
    fireEvent.change(ta, { target: { value: 'please check @src\\main.ts ' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    expect(ta.value).toBe('please check @src\\main.ts ')
  })

  it('removing one file does not unstage a DIFFERENT file that shares its exact alias after a project change (fork GPT review)', async () => {
    // A later pick under a changed project can compute the SAME rel for a
    // DIFFERENT absolute path, so two distinct files can end up sharing the
    // identical literal alias string with only ONE occurrence in the text.
    // Removing one file's chip used to strip that shared occurrence
    // unconditionally, and the reconciliation effect then read the OTHER,
    // untouched file's now-missing alias as stale too and silently dropped
    // it -- deleting an attachment the user never touched.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/main.ts')

    // A DIFFERENT absolute file that relativizes to the SAME rel once the
    // project moves to its parent directory.
    act(() => { store.dispatch(updateSlot({ key: 'slot-a', project: '/repo/other' })) })
    fireEvent.click(await screen.findByText('Add to chat: other/src/main.ts'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    // Still only ONE literal occurrence of the shared alias -- the second
    // pick found it already mentioned and inserted no new text.
    expect((ta.value.match(/@src\/main\.ts/g) ?? []).length).toBe(1)

    // Remove one of the two chips.
    fireEvent.click(screen.getAllByLabelText('Remove')[0])
    // Exactly one chip must survive -- the shared alias text must not have
    // been stripped out from under it.
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    expect(ta.value).toContain('@src/main.ts')
  })

  it('a forward-slash-spelled Windows project still folds separators (fork GPT review)', async () => {
    // A Windows-shaped project (drive letter) spelled with forward slashes
    // (`C:/repo`) has nothing for `normalizeWindowsPath` to rewrite, so
    // `normalizeWindowsPath(project) !== project` silently misclassifies it
    // as POSIX and disables every separator-fold gated on it -- for a
    // project spelled EXACTLY the same way `makeRelative` itself already
    // normalizes an absolute path to. Detecting the drive-letter/UNC prefix
    // directly (`isWindowsShapedPath`) fixes both the record-time derivation
    // and the reconciliation check.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: 'C:/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'please check @src\\main.ts' } })

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts (win project)'))
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('please check @src\\main.ts')

    // Hand-edit the backslash mention to its forward-slash equivalent --
    // same file, same project, different spelling. The chip must survive.
    fireEvent.change(ta, { target: { value: 'please check @src/main.ts' } })
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('please check @src/main.ts')
  })

  it('removing a chip strips its mention even after a separator-only hand-edit on a Windows project (fork GPT review)', async () => {
    // Continuing the scenario above: the chip survives a hand-edit to the
    // OTHER separator spelling (the reconciliation effect folds), but the
    // recorded alias still says the OLD spelling. Removing the chip must
    // still strip the mention as it is NOW spelled in the text -- not just
    // the literal form recorded at pick time -- or a stale, unattached
    // `@rel` reference is left behind and sent.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: 'C:/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'please check @src\\main.ts' } })

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts (win project)'))
    await screen.findByLabelText('Remove')

    // Hand-edit to the forward-slash spelling -- the chip survives this
    // (round 12), but its recorded alias is still the backslash form.
    fireEvent.change(ta, { target: { value: 'please check @src/main.ts' } })
    await screen.findByLabelText('Remove')

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    // No stale, unattached mention left behind.
    expect(ta.value).toBe('please check ')
  })

  it('removing one of two files sharing a Windows alias does not unstage the other after a separator edit (fork GPT review)', async () => {
    // Combines round 11 (two DISTINCT files sharing one literal alias, one
    // text occurrence) with round 12/13 (a Windows-shaped project folding a
    // hand-edited separator): the round-11 guard checked candidates for an
    // EXACT literal match against another staged file's alias, but round
    // 13 tries BOTH separator forms as removal candidates -- the flipped
    // form was never checked against the other file's (unflipped) alias,
    // so it slipped past the guard and stripped the only shared occurrence,
    // unstaging BOTH files instead of just the one being removed.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: 'C:/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'please check @src\\main.ts' } })

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts (win project)'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))

    // A DIFFERENT absolute file under a project that relativizes to the
    // SAME rel -- both files now share the one backslash mention.
    act(() => { store.dispatch(updateSlot({ key: 'slot-a', project: 'C:/repo/other' })) })
    fireEvent.click(await screen.findByText('Add to chat: other/src/main.ts (win project)'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    expect(ta.value).toBe('please check @src\\main.ts')

    // Hand-edit the shared mention to its forward-slash equivalent.
    fireEvent.change(ta, { target: { value: 'please check @src/main.ts' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))

    // Remove one of the two chips.
    fireEvent.click(screen.getAllByLabelText('Remove')[0])
    // Exactly one chip must survive, with the shared mention still intact.
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    expect(ta.value).toBe('please check @src/main.ts')
  })

  it('typing punctuation directly after a mention does not unstage it (fork GPT review)', async () => {
    // `tokenRegex`'s shared trailing boundary requires whitespace or
    // end-of-string, so an entirely ordinary sentence -- "check
    // @file.ts, please" -- reads as "mention gone" the instant the comma
    // is typed, one keystroke into what the user is still typing.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/main.ts')

    // Append a comma directly after the mention, no space in between.
    fireEvent.change(ta, { target: { value: ta.value.trimEnd() + ', thanks' } })
    // The chip must survive -- the file is still clearly referenced.
    await screen.findByLabelText('Remove')
    expect(ta.value).toContain('@src/main.ts,')
  })

  it('removing a chip strips a mention followed directly by punctuation (fork GPT review)', async () => {
    // Continuing the scenario above: the chip survives punctuation typed
    // directly after its mention (round 15's mentionRegex fold), but the
    // remove-chip replace still used the OLD, stricter whitespace-or-end
    // boundary and never matched the punctuated form -- the chip vanished
    // from the list while its `@src/main.ts,` text reference was left
    // behind and would be sent as a stale, unattached mention.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: ta.value.trimEnd() + ', thanks' } })
    await screen.findByLabelText('Remove')

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    // No stale, unattached mention left behind.
    expect(ta.value).not.toContain('@src/main.ts')
  })

  it('a punctuation boundary does not match a mention that is a PREFIX of a longer, different token (fork GPT review)', async () => {
    // `.` is a legal, common mid-filename character (`README.md`), so
    // treating a bare `.` as a sufficient trailing boundary would match
    // `@src/main.ts` as still "present" inside the UNRELATED, longer
    // `@src/main.ts.bak` -- keeping the chip staged (and, if then removed,
    // stripping only the prefix and corrupting the text to `.bak`).
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/main.ts')

    // Extend the mention directly into a DIFFERENT, longer filename, no
    // space in between.
    fireEvent.change(ta, { target: { value: ta.value.trimEnd() + '.bak' } })
    // The original file is no longer genuinely referenced -- the chip
    // must unstage instead of surviving via the prefix collision.
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    // The now-unrelated `@src/main.ts.bak` text must be left untouched.
    expect(ta.value).toContain('@src/main.ts.bak')
  })

  it('removing a file does not corrupt a DIFFERENT staged file whose own mention is a longer, punctuated prefix match (fork GPT review)', async () => {
    // `,` is a legal, common trailing filename character too -- `report`
    // and `report,` can both be genuine, distinct files. Staging both and
    // removing the SHORTER one must not strip the comma out of the
    // LONGER file's own, still-current mention: the punctuation boundary
    // (round 15-17) that lets a comma end a sentence must not ALSO read
    // as "report is still just report," when the text actually says the
    // longer, unrelated `report,`.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    fireEvent.click(await screen.findByText('Add to chat: report,'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Collapse the text to ONLY the longer file's own mention, as if the
    // user had already hand-deleted the shorter file's separate `@report`.
    // Without the fix, the shorter file's chip wrongly stays staged --
    // its punctuation-boundary check reads the longer file's OWN trailing
    // comma as "just punctuation" closing `@report`.
    fireEvent.change(ta, { target: { value: 'please check @report,' } })

    // Exactly one chip must survive -- the longer file, genuinely
    // mentioned -- and its own comma-terminated mention must be intact,
    // not corrupted down to a stray comma by a later remove.
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    expect(ta.value).toBe('please check @report,')
  })

  it('removing a restored alias-less file does not corrupt its restored sibling\'s punctuated mention (fork GPT review)', async () => {
    // A reload restores the text and the staged files from the persisted
    // draft, but the in-memory alias record is rebuilt empty -- so a
    // restored SIBLING contributed nothing to the removal guard. Removing
    // `report` beside the restored `report,` then selected the permissive
    // boundary and stripped `@report` out of the SIBLING's own
    // `@report,` mention, corrupting text the sibling still points at.
    // The guard now folds every sibling's derived current-project rel in,
    // recorded aliases or not (the same fallback the removed side itself
    // uses).
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    const first = await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report,'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    fireEvent.click(await screen.findByText('Add to chat: report'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    // The second pick found `report` already "mentioned" inside `@report,`
    // and inserted nothing -- give each file its own distinct mention by
    // hand, the shape a real draft has.
    fireEvent.change(ta, { target: { value: 'see @report, and @report here' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))

    // Reload: drafts restore, pickedFileTokens (in-memory) is gone --
    // BOTH chips come back alias-less.
    first.unmount()
    const store2 = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store2)
    const ta2 = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(ta2.value).toBe('see @report, and @report here'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))

    // Remove the SHORTER file (staged second, chip index 1).
    fireEvent.click(screen.getAllByLabelText('Remove')[1])
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    // The sibling's own comma-terminated mention survives intact; the
    // shorter file's standalone mention is the one that went.
    expect(ta2.value).toBe('see @report, and here')
  })

  it('a mention wrapped in parens stays staged and strips cleanly on remove (fork GPT review)', async () => {
    // `(@src/main.ts)` -- a mention wrapped in parens -- is an entirely
    // ordinary way to reference a file inline, but the leading `(^|\s)`
    // boundary this shared with `tokenRegex` never recognized it: the
    // reconciliation effect read the file as no-longer-mentioned the
    // moment a wrapping parenthesis made the leading boundary fail,
    // silently dropping a still-intended attachment.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/main.ts')

    // Wrap the mention in parens by hand.
    fireEvent.change(ta, { target: { value: 'please check (@src/main.ts) for bugs' } })
    // The chip must survive -- the file is still clearly referenced.
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('please check (@src/main.ts) for bugs')

    // Remove the chip -- the mention AND its wrapping parens must be
    // stripped cleanly, not left behind as a stray, empty `()` (fork
    // Opus review, round 21 -- `.not.toContain('@src/main.ts')` alone
    // does not catch that regression).
    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    expect(ta.value).toBe('please check for bugs')
  })

  it('a stale, no-longer-staged historical alias does not force a strict boundary onto a currently staged file (fork GPT review)', async () => {
    // Round 18's guard scans EVERY recorded path in `known`, not just
    // currently-staged ones -- entries are deliberately never deleted on
    // an automatic (reconciliation-driven) unstage, only on the chip's
    // own remove control, so `known` can carry a long-abandoned alias for
    // a file that is not attached to anything anymore. Treating that
    // stale history as "another real file to protect" forces the strict,
    // punctuation-free boundary onto an entirely different, CURRENTLY
    // staged file's own ordinary comma-punctuated mention, and wrongly
    // unstages the attachment the user is still actively typing about.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report,'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Hand-delete the mention entirely -- an AUTOMATIC (reconciliation)
    // unstage, not a click on the chip's own remove control, so its
    // alias stays recorded in `known` for revival purposes.
    fireEvent.change(ta, { target: { value: '' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())

    // Stage a DIFFERENT, currently-active file.
    fireEvent.click(await screen.findByText('Add to chat: report'))
    await screen.findByLabelText('Remove')
    expect(ta.value).toContain('@report')

    // Type an entirely ordinary sentence with punctuation directly after
    // the mention -- a DIFFERENT punctuation mark than the stale
    // `report,` history's own, so this can't coincidentally revive it
    // (`report,`'s own literal text is never re-typed here; only its
    // REL is a prefix of this file's, which is all the round-18 guard
    // keys on).
    fireEvent.change(ta, { target: { value: 'please check @report! thanks' } })
    // The chip must survive.
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('please check @report! thanks')
  })

  it('multi-character punctuation clusters and a file:line suffix still count as a mention boundary (fork Opus review)', async () => {
    // A single trailing punctuation character (round 15-17) is too
    // narrow: ordinary sentence-ending clusters (`?!`, a period right
    // after a closing paren) and a file:line reference (`:42`) are all
    // common, everyday ways to punctuate a mention, and none of them
    // satisfied the old boundary -- the reconciliation effect silently
    // unstaged a still-referenced attachment the moment the user typed
    // ordinary punctuation.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: 'is this the right file @src/main.ts?!' } })
    await screen.findByLabelText('Remove')

    fireEvent.change(ta, { target: { value: 'see @src/main.ts:42 for the bug' } })
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('see @src/main.ts:42 for the bug')
  })

  it('shortening a mention unstages the chip: a picked mention is exact-or-nothing (maintainer ruling)', async () => {
    // Pick `@src/main.ts`, rewrite it to the shorter `@main.ts`. Under the
    // atomic-token contract the composer's keyboard can only delete a picked
    // mention whole, never edit inside it, so a shortened spelling is not a
    // form the composer produces -- and no shortened form is ACCEPTED either:
    // the chip visibly unstages, and re-attaching is one pick away. This is
    // what retires the recorded-vs-accepted asymmetry: every accepted
    // spelling is a recorded one, by construction.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/main.ts')

    fireEvent.change(ta, { target: { value: 'please check @main.ts ' } })
    // Unstaged: the shortened spelling matches no recorded alias.
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    expect(ta.value).toBe('please check @main.ts ')
  })

  it('an ambiguous shortened form never keeps a file staged: only the untouched exact mention survives', async () => {
    // With both /repo/src/main.ts and /repo/other/src/main.ts staged, a
    // hand-typed `@main.ts` matches no recorded alias for either file --
    // the edited file unstages, the untouched one stays. Same exact-or-
    // nothing rule as the single-file case above; this pins that a shared
    // trailing segment gives the shortened spelling no special claim.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    fireEvent.click(await screen.findByText('Add to chat: other/src/main.ts'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Rewrite the FIRST file's own mention to the ambiguous short form,
    // leaving the second file's full mention intact.
    fireEvent.change(ta, { target: { value: 'please check @main.ts and @other/src/main.ts ' } })
    // The edited file unstages (ambiguous); the untouched one stays.
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    expect(ta.value).toBe('please check @main.ts and @other/src/main.ts ')
  })

  it('removing a chip whose mention was edited to a file:line form consumes the :line suffix too (fork GPT review)', async () => {
    // `mentionBoundary` admits `:\d+` as a zero-width trailing boundary, so
    // `@src/main.ts:42` keeps the chip staged -- but the remove-chip strip
    // matched only the token itself and left a stray `:42` behind as real
    // composer text.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: 'see @src/main.ts:42 for the bug' } })
    await screen.findByLabelText('Remove')

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    // The whole file:line mention is gone -- no stray `:42` fragment.
    expect(ta.value).not.toContain('@src/main.ts')
    expect(ta.value).not.toContain(':42')
    expect(ta.value).toBe('see for the bug')
  })

  it('a code-formatted or quoted mention stays staged and its wrapper pair strips with the chip (fork GPT review)', async () => {
    // Backtick-wrapping a mention (`@src/main.ts` as inline code) or
    // quoting it is ordinary prose, the same class as the parens round 19
    // admitted -- the old boundaries rejected both, so the reconciliation
    // effect silently unstaged a still-referenced attachment.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: 'see `@src/main.ts` here' } })
    await screen.findByLabelText('Remove')

    fireEvent.change(ta, { target: { value: 'see "@src/main.ts" here' } })
    await screen.findByLabelText('Remove')

    // Removing the chip strips the wrapping pair as a unit, not just the
    // token -- no stray empty quotes left as real message text.
    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    expect(ta.value).not.toContain('@src/main.ts')
    expect(ta.value).toBe('see here')
  })

  it('deleting one sibling mention does not force the strict boundary onto a survivor punctuated in the same edit (fork GPT review)', async () => {
    // `report` and `report,` are both staged. ONE edit deletes `@report,`
    // entirely and punctuates the survivor to `@report!`. The pre-effect
    // staged snapshot still carried the dead sibling's alias, which forced
    // the strict whitespace-only boundary onto `@report!` -- silently
    // dropping BOTH attachments. A dead alias has no text occurrence left
    // to protect, so only aliases live in the current input count.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report'))
    fireEvent.click(await screen.findByText('Add to chat: report,'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: 'keep @report! only' } })
    // The survivor stays staged; only the deleted sibling unstages.
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    expect(ta.value).toBe('keep @report! only')
  })

  it('a mention rewritten to the CURRENT project\'s rel form after re-rooting unstages: no re-derived form is accepted', async () => {
    // Stage under /repo, re-root the slot upward, rewrite the token to the
    // now-valid `@repo/src/main.ts`. The recorded aliases are the ONLY
    // accepted spellings (maintainer ruling): the re-derived current-rel
    // acceptance was an accept-without-record path -- one of the asymmetries
    // that generated the recorded-vs-accepted defect class -- so the chip
    // unstages, and picking the file again under the new root records a
    // fresh exact alias instead.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('@src/main.ts')

    act(() => { store.dispatch(updateSlot({ key: 'slot-a', project: '/' })) })
    fireEvent.change(ta, { target: { value: 'check @repo/src/main.ts ' } })
    // Unstaged: `@repo/src/main.ts` matches no recorded alias.
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    expect(ta.value).toBe('check @repo/src/main.ts ')
  })

  it('inserts a file at the caret without doubling existing leading whitespace', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'before after' } })
    await waitFor(() => expect(ta.value).toBe('before after'))
    ta.setSelectionRange('before '.length, 'before '.length)
    fireEvent.mouseUp(ta)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('before @src/main.ts after')
  })

  it('a mention wrapped WITH a :line suffix stays staged and strips cleanly on remove (fork GPT review)', async () => {
    // `(@src/main.ts:42)` is ordinary prose; the `:line` alternative used to
    // demand whitespace/end straight after the digits, so the closing paren
    // broke the boundary, the chip silently unstaged, and the send omitted
    // the file. The remove control consumes the pair AND the suffix.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: 'see (@src/main.ts:42) here' } })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getByLabelText('Remove')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    expect(ta.value).toBe('see here')
  })

  it('removing a chip never eats a wrapped sibling mention whose own name supplies the :line shape (fork Opus review)', async () => {
    // `a.ts` and `a.ts:42` are two distinct staged files -- the longer rel
    // extends the shorter with `:\d`, which is exactly what flips the strip
    // to strictMentionBoundary and gates `:line`-suffix consumption OFF.
    // The general strip honoured that gate, but the bracket loop hardcoded
    // its own `(?::\d+)?`, so removing `a.ts`'s chip matched `(@a.ts:42)`
    // whole, deleted the SIBLING's wrapped mention, and reconciliation then
    // unstaged the sibling's chip too.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: a.ts'))
    fireEvent.click(await screen.findByText('Add to chat: a.ts:42'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: 'keep (@a.ts:42) and @a.ts done' } })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getAllByLabelText('Remove')).toHaveLength(2)

    // Remove the SHORTER file's chip (staged first).
    fireEvent.click(screen.getAllByLabelText('Remove')[0])
    // Its own bare mention is stripped; the sibling's wrapped mention -- and
    // therefore the sibling's chip -- survive untouched.
    await waitFor(() => expect(ta.value).toBe('keep (@a.ts:42) and done'))
    expect(screen.getAllByLabelText('Remove')).toHaveLength(1)
  })

  it('"Add to chat" on a punctuated existing mention does not insert a duplicate token (fork GPT review)', async () => {
    // The already-mentioned check used the whitespace-only tokenRegex, so
    // `check @src/main.ts.` read as unmentioned and the pick inserted a
    // second `@src/main.ts` beside the punctuated one.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'check @src/main.ts.' } })
    await waitFor(() => expect(ta.value).toBe('check @src/main.ts.'))

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    expect(ta.value).toBe('check @src/main.ts.')
    expect((ta.value.match(/@src\/main\.ts/g) || []).length).toBe(1)
  })

  it('"Add to chat" with the caret parked inside a paste token does not tear the token (fork Opus review)', async () => {
    // A collapsed caret inside a PASTE token is a legal resting place (the
    // click expander's preview case -- the select-snap deliberately leaves
    // it). Splicing the mention there tore the token literal apart:
    // pruneBlocks dropped the block, the draft effect persisted the pruned
    // list, and the pasted content was destroyed unrecoverably. The
    // insertion caret is now clamped to the nearer token edge.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.paste(ta, { clipboardData: { types: ['text/plain'], items: [], getData: () => 'line one\nline two\nline three\nline four\nline five\nline six' } })
    await waitFor(() => expect(ta.value).toMatch(/\[ Paste #1 · \d+ lines \]/))
    const token = ta.value.match(/\[ Paste #1 · \d+ lines \]/)![0]

    // Park the caret INSIDE the token (preview position) and record it.
    const inside = ta.value.indexOf(token) + 4
    ta.setSelectionRange(inside, inside)
    fireEvent.mouseUp(ta)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    await screen.findByLabelText('Remove')
    // The token literal survives intact and the mention landed beside it.
    expect(ta.value).toContain(token)
    expect(ta.value).toContain('@src/main.ts')
  })

  it('a flipped-separator pasted mention still respects the prefix-sibling rule on a Windows project (fork GPT review)', async () => {
    // Both siblings staged under C:\repo (aliases recorded with `/`), both
    // mentions hand-deleted, then the SHORTER alias pasted back in the
    // BACKSLASH spelling with a trailing comma: `@win\report, `. The
    // sibling set used to hold only the recorded `/` spellings, so the
    // flipped candidate matched with the permissive boundary, read the
    // comma as its own boundary, and BOTH files revived -- binding the text
    // to the wrong file at send. The folded sibling set forces the strict
    // boundary and exactly one file (the longer own-alias) revives.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: 'C:\\repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: win/report (win project)'))
    fireEvent.click(await screen.findByText('Add to chat: win/report, (win project)'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: '' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())

    // Paste the LONGER sibling's own mention back, flipped: exactly one revives.
    fireEvent.change(ta, { target: { value: 'check @win\\report, please' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    // Stable across a further keystroke.
    fireEvent.change(ta, { target: { value: 'check @win\\report, please!' } })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getAllByLabelText('Remove')).toHaveLength(1)
  })

  it('typing punctuation after a staged mention does NOT revive the deleted longer sibling (fork GPT review)', async () => {
    // Stage `report` and `report,`, hand-delete `@report,` (alias stays in
    // history by contract), then type a comma after the still-staged
    // `@report`. The candidate `report,` is LONGER than the staged sibling,
    // so the one-directional prefix rule gave it the permissive boundary and
    // it silently re-attached -- and the next keystroke then flipped the
    // staged `report` strict and swapped the attachment. The revival
    // asymmetry guard refuses a candidate whose occurrence merely extends a
    // staged file's live alias with consumable characters.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report'))
    fireEvent.click(await screen.findByText('Add to chat: report,'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Hand-delete the longer sibling's mention: its chip unstages, history stays.
    fireEvent.change(ta, { target: { value: 'see @report and more' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))

    // Type a comma right after the staged mention -- must NOT revive `report,`.
    fireEvent.change(ta, { target: { value: 'see @report, and more' } })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getAllByLabelText('Remove')).toHaveLength(1)

    // A further keystroke must not swap the attachment either.
    fireEvent.change(ta, { target: { value: 'see @report, and more!' } })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getAllByLabelText('Remove')).toHaveLength(1)

    // Identity check: the survivor is `report` -- and because `report,`
    // remains in sibling HISTORY, its strip keeps the strict boundary and
    // deliberately leaves the comma (the comma may be the sibling's own
    // name); `see , and more!` is the contractual output that proves the
    // surviving chip was `report`, not `report,`.
    fireEvent.click(screen.getByLabelText('Remove'))
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    expect(ta.value).toBe('see , and more!')
  })

  it('reviving a pasted mention binds only the file whose OWN alias it is, not a prefix sibling (fork GPT review)', async () => {
    // Stage `report` and `report,`, delete both mentions, paste `@report,`
    // back. Revival's candidates are UNSTAGED entries, so a staged-scoped
    // protecting set is empty exactly when it matters -- the shorter alias
    // then matched the longer file's own mention via the punctuation
    // boundary and BOTH revived, binding the text (and its attachment) to
    // the wrong file at send. Every KNOWN path's live aliases protect
    // revival now: exactly one chip comes back, and it is `report,`'s.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: report'))
    fireEvent.click(await screen.findByText('Add to chat: report,'))
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(2))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: '' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())

    fireEvent.change(ta, { target: { value: 'please check @report, ' } })
    await waitFor(() => expect(screen.getAllByLabelText('Remove')).toHaveLength(1))
    // Still exactly one after a further keystroke (the effect re-runs).
    fireEvent.change(ta, { target: { value: 'please check @report, now' } })
    await new Promise(r => setTimeout(r, 50))
    expect(screen.getAllByLabelText('Remove')).toHaveLength(1)
  })

  it('remove strips only exact recorded aliases; a hand-typed shortened spelling is not this chip\'s text', async () => {
    // A shortened spelling matches no recorded alias, so it is ordinary
    // message text -- the chip's remove control must not eat it. Only the
    // exact `@src/main.ts` the pick inserted is stripped.
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByText('Add to chat: main.ts'))
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await screen.findByLabelText('Remove')

    fireEvent.change(ta, { target: { value: `${ta.value.trim()} plus @main.ts ` } })
    await new Promise(r => setTimeout(r, 50))
    fireEvent.click(screen.getByLabelText('Remove'))

    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
    expect(ta.value).not.toContain('@src/main.ts')
    expect(ta.value).toContain('@main.ts')
  })

  it('a POSIX literal-backslash file is not kept by a basename suffix mention', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a', project: '/repo' }])
    await renderPage(store)

    act(() => { store.dispatch(openActivityPanel()) })
    fireEvent.click(await screen.findByRole('button', { name: /literal POSIX/ }))
    await screen.findByLabelText('Remove')
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    fireEvent.change(ta, { target: { value: 'please check @main.ts ' } })
    await waitFor(() => expect(screen.queryByLabelText('Remove')).toBeNull())
  })
})
