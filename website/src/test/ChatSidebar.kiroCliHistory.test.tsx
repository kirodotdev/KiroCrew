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

function renderSidebar() {
  const slots = [slot('s1', 'Session 1')]
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
    expect(kiroListMock).toHaveBeenCalledWith('', 50, 0)
    expect(row.textContent).toContain('widget')
    expect(row.textContent).toContain('why does the build fail?')
    expect(row.getAttribute('title')).toContain('/home/me/src/widget')
    first.unmount()

    renderSidebar()
    openPane()
    expect(screen.getByRole('tab', { name: S.history_tab_kiro_cli }).getAttribute('aria-selected')).toBe('true')
  })

  it('asks the gateway to search, and offers Clear only on the Crew tab', async () => {
    renderSidebar()
    openPane()
    await waitFor(() => expect(screen.getByRole('button', { name: S.clear })).toBeTruthy(), { timeout: 3000 })
    selectTab(S.history_tab_kiro_cli)
    await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 })
    expect(screen.queryByRole('button', { name: S.clear })).toBeNull()
    fireEvent.change(screen.getByTestId('kiro-cli-search'), { target: { value: 'build' } })
    await waitFor(() => expect(kiroListMock).toHaveBeenCalledWith('build', 50, 0), { timeout: 3000 })
  })

  it('previews a session and continues it in a new chat', async () => {
    kiroContinueMock.mockResolvedValue({
      ok: true, key: 'chat-9-1', title: '⇄ why does the build fail? (from kiro-cli)', messages: 2,
      resume_mode: 'session_load', folder_id: '', project: '/home/me/src/widget',
    })
    const { store } = renderSidebar()
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
  })

  it('says why the chat opened without the session folder before opening it', async () => {
    kiroContinueMock.mockResolvedValue({
      ok: true, key: 'chat-9-2', title: 't', messages: 2, resume_mode: 'session_load',
      project: '', project_skipped: "the session's folder no longer exists",
    })
    renderSidebar()
    openPane()
    selectTab(S.history_tab_kiro_cli)
    fireEvent.click(await screen.findByTestId(`kiro-cli-row-${SID}`, {}, { timeout: 3000 }))
    const dialog = await screen.findByTestId('kiro-cli-preview', {}, { timeout: 3000 })
    await within(dialog).findByText('A missing dependency.', {}, { timeout: 3000 })
    fireEvent.click(within(dialog).getByRole('button', { name: P.continue }))
    const landed = await within(dialog).findByTestId('kiro-cli-preview-landed', {}, { timeout: 3000 })
    expect(landed.textContent).toContain("the session's folder no longer exists")
    fireEvent.click(within(dialog).getByRole('button', { name: P.open_chat }))
    await waitFor(() => expect(screen.queryByTestId('kiro-cli-preview')).toBeNull(), { timeout: 3000 })
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
  })
})
