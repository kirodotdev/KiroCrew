/**
 * Test: the Older Sessions pane's Kiro CLI tab.
 *
 * The pane has two tabs: Crew's own history (what it always listed) and the
 * kiro-cli sessions the owner ran in a terminal. Pinned here:
 *
 *  - the Kiro CLI list is fetched only once that tab is selected, and the tab
 *    choice survives a remount (it is stored);
 *  - each row shows the session's folder, title and age, and a search asks the
 *    gateway (which owns the filter) rather than filtering locally;
 *  - the footer's Clear button, which empties Crew's history, is not offered on
 *    the Kiro CLI tab;
 *  - a row opens a read-only preview whose "Continue in Crew" copies the session
 *    and opens the new chat; when the session's folder could not be used, the
 *    preview stays open to say so before the chat is opened.
 *
 * Mock scaffolding mirrors ChatSidebar.olderSessionsTip.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import en from '../i18n/locales/en.json'

const { sessionsMock, kiroListMock, kiroPageMock, kiroContinueMock } = vi.hoisted(() => ({
  sessionsMock: vi.fn(),
  kiroListMock: vi.fn(),
  kiroPageMock: vi.fn(),
  kiroContinueMock: vi.fn(),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList',
          'renameSlot', 'forkSession', 'sessionsSearch',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatSlots: vi.fn().mockResolvedValue([]),
      chatFolders: vi.fn().mockResolvedValue([]),
      sessions: sessionsMock,
      kiroCliSessions: kiroListMock,
      kiroCliSession: kiroPageMock,
      continueKiroCliSession: kiroContinueMock,
    },
  }
})

// The hand-off to the agent is stubbed to "staged": what is pinned here is that
// the dialog gets out of the way of the chat the hand-off opens.
vi.mock('../utils/errorReport', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/errorReport')>()
  return { ...actual, sendErrorToChat: vi.fn(() => true) }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import { switchSlot } from '../store/chatSlice'
import { resetHistorySource } from '../pages/chat-sidebar/KiroCliHistory'
import type { ChatSlot, ChatHistoryItem } from '../types'
import type { RootState } from '../store'

const S = en.pages.chatSidebar
const P = en.components.kiroCliSessionPreview
const SID = '11111111-2222-4333-8444-555555555555'

const slot = (key: string, title?: string): ChatSlot => ({
  key, title: title ?? key, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const histItem = (key: string, title: string): ChatHistoryItem => ({
  key, title, last_ts: '2026-01-01T00:00:00Z',
} as unknown as ChatHistoryItem)

const terminalRow = {
  id: SID, title: 'why does the build fail?', cwd: '/home/me/src/widget',
  created_at: '2026-10-01T10:00:00Z', updated_at: '2026-10-02T10:00:00Z',
}

function renderSidebar({ onSelectSlot }: { onSelectSlot?: (key: string) => void } = {}) {
  const slots = [slot('s1', 'Session 1'), slot('s2', 'Session 2')]
  const history = [histItem('h1', 'A closed conversation')]
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 's1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history, historyHasMore: false, historyOffset: history.length,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={'s1'}
              unreadSlots={[]}
              history={history}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
              onSelectSlot={onSelectSlot}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store }
}

const openPane = () => fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
// Radix tabs select on mousedown (pointer) and on focus+keys; mousedown is the
// gesture jsdom can drive.
const selectTab = (name: string) => fireEvent.mouseDown(screen.getByRole('tab', { name }), { button: 0 })

describe('ChatSidebar — the Older Sessions pane lists terminal kiro-cli sessions', () => {
  beforeEach(() => {
    localStorage.clear()
    resetHistorySource()
    sessionsMock.mockReset().mockResolvedValue({ sessions: [histItem('h1', 'A closed conversation')], has_more: false })
    kiroListMock.mockReset().mockResolvedValue({ sessions: [terminalRow], total: 1, has_more: false })
    kiroPageMock.mockReset().mockResolvedValue({
      ...terminalRow,
      messages: [
        { role: 'user', content: 'why does the build fail?', ts: '', truncated: false },
        { role: 'assistant', content: 'A missing dependency.', ts: '', truncated: false },
      ],
      start: 0, total: 2, skipped: 0, has_transcript: true,
    })
    kiroContinueMock.mockReset()
    window.history.replaceState({}, '', '/chat')
  })
  afterEach(() => { window.history.replaceState({}, '', '/') })

  it('fetches the terminal sessions only once the Kiro CLI tab is chosen, and remembers the choice', async () => {
    const first = renderSidebar()
    openPane()
    expect(screen.getByRole('tab', { name: S.history_tab_crew }).getAttribute('aria-selected')).toBe('true')
    expect(kiroListMock).not.toHaveBeenCalled()
    selectTab(S.history_tab_kiro_cli)
    const row = await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 })
    expect(kiroListMock).toHaveBeenCalledWith('', 50, null)
    expect(row.textContent).toContain('widget')
    expect(row.textContent).toContain('why does the build fail?')
    expect(row.getAttribute('title')).toContain('/home/me/src/widget')
    first.unmount()
    // A reload: the held choice is gone and only storage carries it.
    resetHistorySource()

    renderSidebar()
    openPane()
    expect(screen.getByRole('tab', { name: S.history_tab_kiro_cli }).getAttribute('aria-selected')).toBe('true')
  })

  it('switches tabs when the browser refuses storage', async () => {
    // Refused for this pane's key only: the rest of the sidebar reads storage
    // with no guard this test is about.
    const realGet = Storage.prototype.getItem
    const realSet = Storage.prototype.setItem
    const deny = (key: string) => { if (key === 'mc-history-source') throw new DOMException('denied', 'SecurityError') }
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(function (this: Storage, key: string) { deny(key); return realGet.call(this, key) })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key: string, value: string) { deny(key); realSet.call(this, key, value) })
    try {
      renderSidebar()
      openPane()
      selectTab(S.history_tab_kiro_cli)
      expect(screen.getByRole('tab', { name: S.history_tab_kiro_cli }).getAttribute('aria-selected')).toBe('true')
      await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 })
    } finally {
      getItem.mockRestore()
      setItem.mockRestore()
    }
  })

  it('reads the list and the conversation again each time they are opened', async () => {
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    fireEvent.click(within(dialog).getByTestId('kiro-cli-preview-close'))
    await waitFor(() => expect(screen.queryByTestId('kiro-cli-preview')).toBeNull(), { timeout: 3000 })
    // A turn typed in the terminal since: no Crew event says so.
    kiroPageMock.mockResolvedValue({
      ...terminalRow,
      messages: [{ role: 'user', content: 'and on Windows?', ts: '', truncated: false }],
      start: 0, total: 1, skipped: 0, has_transcript: true,
    })
    fireEvent.click(screen.getByTestId(`kiro-cli-row-${SID}`))
    const reopened = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(reopened).findByText('and on Windows?', {}, { timeout: 3000 })
    // Switching away and back reads the list again too.
    const listCalls = kiroListMock.mock.calls.length
    fireEvent.click(within(reopened).getByTestId('kiro-cli-preview-close'))
    selectTab(S.history_tab_crew)
    selectTab(S.history_tab_kiro_cli)
    await waitFor(() => expect(kiroListMock.mock.calls.length).toBeGreaterThan(listCalls), { timeout: 3000 })
  })

  it('asks the gateway to search, and offers Clear only on the Crew tab', async () => {
    renderSidebar()
    openPane()
    await waitFor(() => expect(screen.getByRole('button', { name: S.clear })).toBeTruthy(), { timeout: 3000 })
    selectTab(S.history_tab_kiro_cli)
    await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 })
    expect(screen.queryByRole('button', { name: S.clear })).toBeNull()
    fireEvent.change(screen.getByTestId('kiro-cli-search'), { target: { value: 'build' } })
    await waitFor(() => expect(kiroListMock).toHaveBeenCalledWith('build', 50, null), { timeout: 3000 })
  })

  it('loads each further page after the last row loaded', async () => {
    const page = (start: number, n: number, more: boolean) => ({
      sessions: Array.from({ length: n }, (_, i) => ({
        ...terminalRow,
        id: `${String(start + i).padStart(8, '0')}-2222-4333-8444-555555555555`,
        title: `session ${start + i}`,
      })),
      total: 120, has_more: more,
    })
    kiroListMock.mockReset()
      .mockResolvedValueOnce(page(0, 50, true))
      .mockResolvedValueOnce(page(50, 50, true))
      .mockResolvedValueOnce(page(100, 20, false))
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId('kiro-cli-load-more', {}, { timeout: 3000 }))
    // The next page resumes after the last row shown, not after a count of rows,
    // so a session a terminal touched meanwhile cannot serve that row again.
    const rowAt = (i: number) => expect.objectContaining({ id: `${String(i).padStart(8, '0')}-2222-4333-8444-555555555555`, updated_at: terminalRow.updated_at })
    await waitFor(() => expect(kiroListMock).toHaveBeenCalledWith('', 50, rowAt(49)), { timeout: 3000 })
    await screen.findByText('session 99', {}, { timeout: 3000 })
    fireEvent.click(screen.getByTestId('kiro-cli-load-more'))
    await waitFor(() => expect(kiroListMock).toHaveBeenCalledWith('', 50, rowAt(99)), { timeout: 3000 })
    // The last page leaves every row listed and no Load more to press.
    await screen.findByText('session 119', {}, { timeout: 3000 })
    expect(screen.getAllByTestId(/^kiro-cli-row-/)).toHaveLength(120)
    expect(screen.queryByTestId('kiro-cli-load-more')).toBeNull()
  })

  it('previews a session and continues it in a new chat', async () => {
    kiroContinueMock.mockResolvedValue({
      ok: true, key: 'chat-9-1', title: '⇄ why does the build fail? (from kiro-cli)', messages: 2,
      resume_mode: 'session_load', folder_id: '', project: '/home/me/src/widget',
    })
    // The host's own hand-off: the embedded sessions list navigates through it.
    const onSelectSlot = vi.fn()
    const { store } = renderSidebar({ onSelectSlot })
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    expect(kiroPageMock).toHaveBeenCalledWith(SID, undefined, 100)
    fireEvent.click(within(dialog).getByRole('button', { name: P.continue }))
    await waitFor(() => expect(screen.queryByTestId('kiro-cli-preview')).toBeNull(), { timeout: 3000 })
    expect(kiroContinueMock).toHaveBeenCalledWith(SID)
    // The copy is the chat the sidebar now points at.
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-9-1'), { timeout: 3000 })
    expect(onSelectSlot).toHaveBeenCalledWith('chat-9-1')
  })

  it('leaves a chat the person opened while the copy was made where it is', async () => {
    let land!: (r: unknown) => void
    kiroContinueMock.mockReturnValue(new Promise(resolve => { land = resolve }))
    const onSelectSlot = vi.fn()
    const { store } = renderSidebar({ onSelectSlot })
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    fireEvent.click(within(dialog).getByRole('button', { name: P.continue }))
    await waitFor(() => expect(kiroContinueMock).toHaveBeenCalledWith(SID), { timeout: 3000 })
    // The person opens another chat before the copy lands (a shortcut or the
    // palette: the dialog itself cannot be dismissed meanwhile). This is the
    // dispatch a session row's click makes.
    store.dispatch(switchSlot({ key: 's2', announceOnMissing: true, gesture: true }))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('s2'), { timeout: 3000 })
    onSelectSlot.mockClear()
    land({ ok: true, key: 'chat-9-3', title: 'copy', messages: 2, resume_mode: 'session_load', folder_id: '', project: '/home/me/src/widget' })
    // Listed, but the chat chosen afterwards stays the one shown.
    await waitFor(() => expect(store.getState().dashboard.slots.some(s => s.key === 'chat-9-3')).toBe(true), { timeout: 3000 })
    expect(store.getState().chat.activeSlot).toBe('s2')
    expect(onSelectSlot).not.toHaveBeenCalledWith('chat-9-3')
    // The dialog offers to open the copy instead.
    await within(dialog).findByTestId('kiro-cli-preview-open', {}, { timeout: 3000 })
  })

  it('cannot be dismissed while the copy is made, so a refusal is always shown', async () => {
    let refuse!: (e: Error) => void
    kiroContinueMock.mockReturnValue(new Promise((_resolve, reject) => { refuse = reject }))
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    fireEvent.click(within(dialog).getByRole('button', { name: P.continue }))
    await waitFor(() => expect(within(dialog).getByTestId('kiro-cli-preview-close').hasAttribute('disabled')).toBe(true), { timeout: 3000 })
    // Neither Escape nor the dialog's X closes it while the request is out.
    fireEvent.keyDown(dialog, { key: 'Escape' })
    // The only Close left is the footer's, and it is disabled: the X is gone.
    expect(within(dialog).getAllByRole('button', { name: en.components.dialog.close })).toEqual([within(dialog).getByTestId('kiro-cli-preview-close')])
    expect(screen.getByTestId('kiro-cli-preview')).toBeTruthy()
    refuse(new Error('this kiro-cli session is still being written'))
    const err = await within(dialog).findByTestId('kiro-cli-preview-continue-error', {}, { timeout: 3000 })
    expect(err.textContent).toContain('still being written')
    expect(within(dialog).getByTestId('kiro-cli-preview-close').hasAttribute('disabled')).toBe(false)
  })

  it('says why the chat opened without the session folder before opening it', async () => {
    kiroContinueMock.mockResolvedValue({
      ok: true, key: 'chat-9-2', title: 't', messages: 2, resume_mode: 'session_load',
      project: '', project_skipped: "Project directory must be an existing directory",
    })
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    fireEvent.click(within(dialog).getByRole('button', { name: P.continue }))
    const landed = await within(dialog).findByTestId('kiro-cli-preview-landed', {}, { timeout: 3000 })
    expect(landed.textContent).toContain("Project directory must be an existing directory")
    // A failure the gateway reported, so it is an ErrorNotice: titled, with the
    // agent hand-off, and the copy is still one click away.
    expect(landed.textContent).toContain(P.opened_without_folder)
    expect(within(landed).getByRole('button', { name: /agent/i })).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: P.open_chat }))
    await waitFor(() => expect(screen.queryByTestId('kiro-cli-preview')).toBeNull(), { timeout: 3000 })
  })

  // A transcript-only copy (another harness, or MCP Tool Search on, the
  // default) is a working result, not a failure: the chat opens like any other
  // Continue, and no red notice holds it back.
  it.each(['kiro_cli_backend_required', 'tool_search_replays_transcript'] as const)(
    'opens a transcript-only copy (%s) like any other',
    async (code) => {
      kiroContinueMock.mockResolvedValue({
        ok: true, key: 'chat-9-4', title: 't — transcript only', messages: 2, resume_mode: 'transcript',
        folder_id: '', project: '/home/me/src/widget', context_skipped: code,
      })
      const onSelectSlot = vi.fn()
      const { store } = renderSidebar({ onSelectSlot })
      openPane()
      selectTab(S.history_tab_kiro_cli)
      fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
      const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
      await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
      fireEvent.click(within(dialog).getByRole('button', { name: P.continue }))
      await waitFor(() => expect(screen.queryByTestId('kiro-cli-preview')).toBeNull(), { timeout: 3000 })
      expect(store.getState().chat.activeSlot).toBe('chat-9-4')
      expect(onSelectSlot).toHaveBeenCalledWith('chat-9-4')
    },
  )

  it('keeps the transcript-only line neutral when the dialog stays for the folder notice', async () => {
    kiroContinueMock.mockResolvedValue({
      ok: true, key: 'chat-9-5', title: 't — transcript only', messages: 2, resume_mode: 'transcript',
      folder_id: '', project: '', project_skipped: 'Project directory must be an existing directory',
      context_skipped: 'tool_search_replays_transcript',
    })
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    fireEvent.click(within(dialog).getByRole('button', { name: P.continue }))
    // The folder is a failure, so it is the ErrorNotice; the line saying what
    // the chat kept is plain text with no alert role and no agent hand-off,
    // and it never shows the reason code.
    await within(dialog).findByTestId('kiro-cli-preview-landed', {}, { timeout: 3000 })
    const line = within(dialog).getByTestId('kiro-cli-preview-state')
    expect(line.textContent).toBe(P.state_needs_tool_search_off)
    expect(line.closest('[role="alert"]')).toBeNull()
    expect(within(line).queryByRole('button')).toBeNull()
    expect(line.textContent).not.toContain('tool_search_replays_transcript')
  })

  // Before the click the preview says what the new chat will keep, from the
  // code the gateway answers for this machine: the setting that would change
  // it, or, where the machine's kiro-cli runs chats on Kiro Agent, that none can.
  it.each([
    [undefined, P.state_kept],
    ['tool_search_replays_transcript', P.state_needs_tool_search_off],
    ['kiro_cli_backend_required', P.state_needs_harness],
    ['kiro_cli_state_unavailable', P.state_unavailable.replace('{{productName}}', 'Kiro Crew')],
  ] as const)('says before Continue what the new chat keeps (%s)', async (code, expected) => {
    kiroPageMock.mockResolvedValue({
      ...terminalRow,
      messages: [
        { role: 'user', content: 'why does the build fail?', ts: '', truncated: false },
        { role: 'assistant', content: 'A missing dependency.', ts: '', truncated: false },
      ],
      start: 0, total: 2, skipped: 0, has_transcript: true,
      ...(code ? { context_skipped: code } : {}),
    })
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    const line = within(dialog).getByTestId('kiro-cli-preview-state')
    expect(line.textContent).toBe(expected)
    expect(within(dialog).getByTestId('kiro-cli-preview-hint').textContent).toContain(P.continue_hint)
    if (code === 'kiro_cli_state_unavailable') {
      // No setting can change it here, so none is named.
      expect(line.textContent).not.toContain('Agent Harness')
      expect(line.textContent).not.toContain('MCP Tool Search')
    }
  })

  it('reports records it could not read through the error notice', async () => {
    kiroPageMock.mockResolvedValue({
      ...terminalRow,
      messages: [{ role: 'user', content: 'why does the build fail?', ts: '', truncated: false }],
      start: 0, total: 1, skipped: 2, has_transcript: true,
    })
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    const notice = await within(dialog).findByTestId('kiro-cli-preview-skipped', {}, { timeout: 3000 })
    expect(notice.textContent).toContain(P.skipped)
    expect(within(notice).getByRole('button', { name: /agent/i })).toBeTruthy()
  })

  it('shows the gateway refusal when a session cannot be continued', async () => {
    kiroContinueMock.mockRejectedValue(new Error('this kiro-cli session is still being written'))
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    fireEvent.click(within(dialog).getByRole('button', { name: P.continue }))
    const err = await within(dialog).findByTestId('kiro-cli-preview-continue-error', {}, { timeout: 3000 })
    expect(err.textContent).toContain('still being written')
    // Asking the agent about it closes the dialog, which would otherwise sit
    // over the chat the hand-off opens.
    fireEvent.click(within(err).getByRole('button', { name: /agent/i }))
    await waitFor(() => expect(screen.queryByTestId('kiro-cli-preview')).toBeNull(), { timeout: 3000 })
  })
})
