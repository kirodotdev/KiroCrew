/**
 * Test: per-machine groups in the Sessions list (preview flag on).
 *
 * The RFC's exit criteria, one case each:
 *  - one connected crew: two groups, `Local` and the crew, each row in its own;
 *  - collapsing the crew group keeps the selection;
 *  - a disconnect keeps the crew's last rows, dimmed, with no online badge;
 *  - no crew group: the sidebar renders exactly what it renders with the flag off.
 *
 * Mock scaffolding mirrors ChatSidebar.instanceSessionsMerge.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { requestSlotReveal } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'
import { PREVIEW_INSTANCE_SESSIONS, PREVIEW_REMOTE_CREW_CHAT } from '../utils/previewFlags'
import en from '../i18n/locales/en.json'
import enManual from '../i18n/locales/en.manual.json'

const { instanceChatSlotsMock, listInstancesMock, chatFoldersMock, crewPeerGetMock } = vi.hoisted(() => ({
  instanceChatSlotsMock: vi.fn(),
  listInstancesMock: vi.fn(),
  chatFoldersMock: vi.fn(),
  crewPeerGetMock: vi.fn(),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession', 'connectInstance', 'sessionsSearch',
          'instancesSearchSessions',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: chatFoldersMock,
      tagColumns: vi.fn().mockResolvedValue([]),
      kirocrewConfig: vi.fn().mockResolvedValue({}),
      listInstances: listInstancesMock,
      instanceChatSlots: instanceChatSlotsMock,
      crewPeerGet: crewPeerGetMock,
      // A gateway without the Shared Crew routes.
      sharedCrewCapabilities: vi.fn().mockResolvedValue(null),
    },
  }
})

vi.mock('../hooks/useSelectInstance', () => ({
  useSelectInstance: () => ({
    selectInstance: vi.fn(),
    connectMutation: { mutate: vi.fn(), isPending: false },
  }),
}))

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
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const S = en.pages.chatSidebar

// One instant stands in for "now" in this file. A row's time label is the HH:MM of its
// own stamp, so rows stamped from the live clock let two renders of the same fixture
// land either side of a minute boundary and differ in a label the test never compares.
// Fixtures are built here from PIN, at collection time; the hooks below pin what the
// component reads at render. PIN is LOCAL midday, the point farthest from both midnight
// edges in any timezone, in mid-January so no DST change falls near it.
const PIN = new Date(2026, 0, 15, 12, 0, 0)
const ago = (ms: number) => new Date(PIN.getTime() - ms).toISOString()

const crew = (state: string) => ({ id: 'inst-a', name: 'astro', status: { state } })
const PEER_ROW = {
  key: 'chat-9', row_identity: 'inst-a:chat-9', title: 'REMOTE peer row',
  last_turn_ts: ago(120_000), agent: 'default',
}

const liveSlot = (key: string, title: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '',
  last_turn_ts: ago(60_000), ...extra,
} as ChatSlot)

function renderSidebar({ activeSlot = 's-local', relay = false, relayFolder, relayParent, relayExtra, onSelectSlot, onOpenPeerSession }: {
  activeSlot?: string
  /** Adds a LOCAL slot whose turns run on `inst-a` (`executor: 'remote'`). */
  relay?: boolean
  /** Files that relay slot in a folder. */
  relayFolder?: string
  /** Makes the plain local slot the relay slot's creator, so the conductor lane exists. */
  relayParent?: boolean
  /** More fields on that relay slot. */
  relayExtra?: Partial<ChatSlot>
  onSelectSlot?: (key: string) => void
  onOpenPeerSession?: (instanceId: string, key: string) => void
} = {}) {
  const slots = [
    liveSlot('s-local', 'LOCAL plain slot'),
    ...(relay ? [liveSlot('s-relay', 'RELAY slot', {
      executor: 'remote', instance_id: 'inst-a', ...(relayFolder ? { folder_id: relayFolder } : {}),
      ...(relayParent ? { parent: { slot: 's-local', key: 's-local' } } : {}),
      ...relayExtra,
    } as Partial<ChatSlot>)] : []),
  ]
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
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
            <ChatSidebar slots={slots} activeSlot={activeSlot} unreadSlots={[]} history={[]}
              historyHasMore={false} defaultAgent={'default'} installedAgents={[]} onSelectSlot={onSelectSlot}
              onOpenPeerSession={onOpenPeerSession} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store, qc }
}

const rowIn = (scope: HTMLElement, title: string) => within(scope).queryByText(title)

// Fake only `Date`, so real timers keep driving findBy*, waitFor and the query client.
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(PIN)
})
afterEach(() => {
  vi.useRealTimers()
})

describe('ChatSidebar – per-machine groups', () => {
  beforeEach(() => {
    localStorage.clear()
    chatFoldersMock.mockReset().mockResolvedValue([])
    instanceChatSlotsMock.mockReset().mockResolvedValue([PEER_ROW])
    listInstancesMock.mockReset().mockResolvedValue({ instances: [crew('connected')] })
    crewPeerGetMock.mockReset().mockResolvedValue([])
  })

  it('renders Local and one group per connected crew, each row in its own group', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    renderSidebar({ relay: true })

    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'REMOTE peer row')).not.toBeNull())
    expect(screen.getByTestId('machine-group-local')).toHaveTextContent(S.machine_group_local)
    expect(screen.getAllByTestId(/^crew-group-inst-/)
      .filter(el => el.tagName === 'SECTION')).toHaveLength(1)
    expect(screen.getByTestId('crew-group-badge-inst-a')).toHaveTextContent(S.crew_status_online)
    // A relay slot renders inside its crew's group; a plain local slot stays out.
    expect(rowIn(group, 'RELAY slot')).not.toBeNull()
    expect(rowIn(group, 'LOCAL plain slot')).toBeNull()
    expect(screen.getByText('LOCAL plain slot')).toBeInTheDocument()
  })

  it('shows the crew groups with only Chat on a crew on', async () => {
    // The two-machine bug: "New chat on crew" was on, the old instance-sessions
    // switch was off, so the chat opened and the sidebar had no group for it.
    localStorage.setItem(PREVIEW_REMOTE_CREW_CHAT, '1')
    renderSidebar({ relay: true })

    // Two chained queries on a cold cache (the instance list, then that crew's
    // peer slots) before the row renders, so the wait names its own ceiling.
    const CHAINED_QUERY_TIMEOUT_MS = 5_000
    const group = await screen.findByTestId('crew-group-inst-a', {}, { timeout: CHAINED_QUERY_TIMEOUT_MS })
    await waitFor(() => expect(rowIn(group, 'REMOTE peer row')).not.toBeNull(), { timeout: CHAINED_QUERY_TIMEOUT_MS })
    expect(rowIn(group, 'RELAY slot')).not.toBeNull()
  })

  it('keeps the selection when the crew group holding it collapses', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const onSelectSlot = vi.fn()
    const { store } = renderSidebar({ relay: true, activeSlot: 's-relay', onSelectSlot })

    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'RELAY slot')).not.toBeNull())
    const toggle = screen.getByTestId('crew-group-toggle-inst-a')
    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within(group).getByText('RELAY slot').closest('[aria-hidden="true"]')).not.toBeNull()
    expect(onSelectSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe('s-relay')

    fireEvent.click(toggle)
    const relayRow = within(group).getByText('RELAY slot').closest('[data-session-row]')
    expect(relayRow).toHaveAttribute('aria-current', 'true')
  })

  it('keeps a disconnected crew\'s last rows, dimmed, under an Offline badge', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const { qc } = renderSidebar()

    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'REMOTE peer row')).not.toBeNull())
    const fetches = instanceChatSlotsMock.mock.calls.length

    listInstancesMock.mockResolvedValue({ instances: [crew('disconnected')] })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['instances'] }) })

    await waitFor(() => expect(screen.getByTestId('crew-group-inst-a')).toHaveAttribute('data-offline'))
    const body = screen.getByTestId('crew-group-body-inst-a')
    expect(rowIn(body, 'REMOTE peer row')).not.toBeNull()
    expect(body).toHaveClass('opacity-50')
    expect(screen.getByTestId('crew-group-inst-a')).toHaveTextContent(S.crew_group_offline)
    expect(screen.getByTestId('crew-group-badge-inst-a')).toHaveTextContent(S.crew_status_offline)
    expect(screen.getByTestId('crew-group-badge-inst-a')).not.toHaveTextContent(S.crew_status_online)
    // The row's click hint says the crew is unreachable, not "open here".
    expect(within(body).getByTestId('session-peer-not-open-here')).toHaveTextContent(S.crew_row_offline)
    expect(within(body).queryByText(enManual.pages.chatSidebar.thinking)).toBeNull()
    // No row count while offline: the cached rows are not a current tally.
    expect(screen.getByTestId('crew-group-toggle-inst-a')).not.toHaveTextContent(/\d/)
    // The cached rows are the last answer: nothing asks the offline crew again.
    expect(instanceChatSlotsMock.mock.calls.length).toBe(fetches)
  })

  it('drops the per-row crew chip inside a crew group', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    renderSidebar({ relay: true })
    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'REMOTE peer row')).not.toBeNull())
    expect(within(group).queryAllByTestId('remote-crew-chip')).toHaveLength(0)
    // The click-outcome line names no machine either; the header does.
    expect(within(group).getByTestId('session-peer-not-open-here')).toHaveTextContent(S.crew_row_open_here)
    expect(within(group).queryByText(/On astro/)).toBeNull()
  })

  it('opens a collapsed crew group to reveal a session inside it', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const { store } = renderSidebar({ relay: true })
    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'RELAY slot')).not.toBeNull())
    const toggle = screen.getByTestId('crew-group-toggle-inst-a')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    act(() => { store.dispatch(requestSlotReveal('s-relay')) })
    await waitFor(() => expect(toggle).toHaveAttribute('aria-expanded', 'true'))
  })

  it('does not say "no sessions match" when the matches sit in a crew group', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    renderSidebar({ relay: true })
    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'RELAY slot')).not.toBeNull())

    fireEvent.change(screen.getByPlaceholderText(S.search_sessions), { target: { value: 'RELAY' } })
    await waitFor(() => expect(rowIn(group, 'RELAY slot')).not.toBeNull())
    expect(screen.queryByText('LOCAL plain slot')).toBeNull()
    expect(screen.queryByText(S.no_sessions_match)).toBeNull()
  })

  it('says nothing about "no sessions match" in the conductor lane when matches sit in a crew group', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    localStorage.setItem('mc-sidebar-lane', 'conductor')
    renderSidebar({ relay: true, relayParent: true })
    await screen.findByTestId('conductor-view-lane')
    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'RELAY slot')).not.toBeNull())

    fireEvent.change(screen.getByPlaceholderText(S.search_sessions), { target: { value: 'RELAY' } })
    await waitFor(() => expect(screen.queryByText('LOCAL plain slot')).toBeNull())
    expect(rowIn(group, 'RELAY slot')).not.toBeNull()
    expect(screen.queryByText(S.no_sessions_match)).toBeNull()
  })

  it('opens a collapsed crew group to reveal a session a search was hiding', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const { store } = renderSidebar({ relay: true })
    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'RELAY slot')).not.toBeNull())
    const toggle = screen.getByTestId('crew-group-toggle-inst-a')
    fireEvent.click(toggle)
    fireEvent.change(screen.getByPlaceholderText(S.search_sessions), { target: { value: 'plain' } })
    await waitFor(() => expect(screen.queryByTestId('crew-group-inst-a')).toBeNull())

    act(() => { store.dispatch(requestSlotReveal('s-relay')) })
    await waitFor(() => expect(screen.getByTestId('crew-group-toggle-inst-a')).toHaveAttribute('aria-expanded', 'true'))
  })

  it('hides a crew-group row whose folder the folder filter hides', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    localStorage.setItem('mc-flat-hidden-folders', JSON.stringify(['f-hidden']))
    chatFoldersMock.mockResolvedValue([{ id: 'f-hidden', name: 'Hidden', parent_id: null, collapsed: false, order: 0 }])
    renderSidebar({ relay: true, relayFolder: 'f-hidden' })
    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'REMOTE peer row')).not.toBeNull())
    expect(rowIn(group, 'RELAY slot')).toBeNull()
  })

  it('shows a crew\'s tunnel error through the shared error notice', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    listInstancesMock.mockResolvedValue({ instances: [{ ...crew('error'), status: { state: 'error', error: 'ssh: connect timed out' } }] })
    renderSidebar({ relay: true })
    expect(await screen.findByTestId('crew-group-badge-inst-a')).toHaveTextContent(S.crew_status_error)
    const notice = await screen.findByTestId('crew-group-error-inst-a')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('ssh: connect timed out')
  })

  it('keeps an open relay-slot rename and its draft across a crew disconnect', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const { qc } = renderSidebar({ relay: true })
    const group = await screen.findByTestId('crew-group-inst-a')
    await waitFor(() => expect(rowIn(group, 'RELAY slot')).not.toBeNull())

    fireEvent.doubleClick(within(group).getByText('RELAY slot'), { detail: 2 })
    const editor = within(group).getByRole('textbox')
    fireEvent.change(editor, { target: { value: 'half-typed title' } })

    listInstancesMock.mockResolvedValue({ instances: [crew('disconnected')] })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['instances'] }) })
    await waitFor(() => expect(screen.getByTestId('crew-group-inst-a')).toHaveAttribute('data-offline'))

    expect(within(screen.getByTestId('crew-group-inst-a')).getByRole('textbox')).toHaveValue('half-typed title')
  })

  it('badges a reconnecting crew and an erroring one from the tunnel state', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    listInstancesMock.mockResolvedValue({ instances: [crew('connecting')] })
    renderSidebar({ relay: true })
    expect(await screen.findByTestId('crew-group-badge-inst-a')).toHaveTextContent(S.crew_status_reconnecting)
  })

  it('renders the same sidebar as the flag-off one when no crew has a group', async () => {
    // A crew that is not connected and owns no row gets no group, so the
    // preview draws no group chrome at all.
    listInstancesMock.mockResolvedValue({ instances: [crew('disconnected')] })
    const normalize = (html: string) => html
      .replace(/(«|:)r[0-9a-z]+(»|:)/g, 'ID')
      .replace(/DndLiveRegion-\d+|DndDescribedBy-\d+/g, 'DND')
      // Row entry animation: a mid-fade frame is timing, not structure.
      .replace(/ style="opacity:[^"]*"/g, '')

    const off = renderSidebar()
    await screen.findByText('LOCAL plain slot')
    const offHtml = normalize(off.container.innerHTML)
    off.unmount()

    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const on = renderSidebar()
    await screen.findByText('LOCAL plain slot')
    await waitFor(() => expect(listInstancesMock).toHaveBeenCalled())
    expect(screen.queryByTestId('machine-group-local')).toBeNull()
    expect(normalize(on.container.innerHTML)).toBe(offHtml)
    expect(instanceChatSlotsMock).not.toHaveBeenCalled()
  })
})

describe('ChatSidebar – spaces inside a crew group', () => {
  // Every space waits on a chain: the instances query, then the crew's slots and
  // folders queries it enables. Bounded explicitly so a loaded runner fails on
  // the chain, not on the default deadline.
  const CHAIN_WAIT = { timeout: 5_000 }
  const peerRow = (key: string, title: string, folder?: string) => ({
    ...PEER_ROW, key, row_identity: `inst-a:${key}`, title, ...(folder ? { folder_id: folder } : {}),
  })
  // The crew's own tree: Work > Sidebar, plus an empty Archive that holds no chat.
  const PEER_FOLDERS = [
    { id: 'f-work', name: 'Work', order: 1 },
    { id: 'f-sub', name: 'Sidebar', order: 0, parent_id: 'f-work' },
    { id: 'f-arch', name: 'Archive', order: 2 },
  ]

  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    chatFoldersMock.mockReset().mockResolvedValue([])
    listInstancesMock.mockReset().mockResolvedValue({ instances: [crew('connected')] })
    instanceChatSlotsMock.mockReset().mockResolvedValue([
      peerRow('c-loose', 'LOOSE chat'),
      peerRow('c-work', 'WORK chat', 'f-work'),
      peerRow('c-sub', 'SUB chat', 'f-sub'),
      // Filed in a folder the crew did not list: shown unfiled, never lost.
      peerRow('c-gone', 'GONE-folder chat', 'f-missing'),
    ])
    crewPeerGetMock.mockReset().mockImplementation((_id: string, path: string) =>
      Promise.resolve(path === 'api/chat/folders' ? PEER_FOLDERS : []))
  })

  it('nests each chat under the crew\'s own folder, unfiled chats last', async () => {
    renderSidebar()
    const work = await screen.findByTestId('crew-space-inst-a-f-work', {}, CHAIN_WAIT)
    expect(crewPeerGetMock).toHaveBeenCalledWith('inst-a', 'api/chat/folders')
    const sub = within(work).getByTestId('crew-space-inst-a-f-sub')
    expect(within(work).getByTestId('crew-space-toggle-inst-a-f-work')).toHaveTextContent(/Work\s*2/)
    expect(rowIn(sub, 'SUB chat')).not.toBeNull()
    expect(rowIn(work, 'WORK chat')).not.toBeNull()
    // A space with no chat below it is left out.
    expect(screen.queryByTestId('crew-space-inst-a-f-arch')).toBeNull()
    // Unfiled rows come after every space, outside them.
    const body = screen.getByTestId('crew-group-body-inst-a')
    expect(rowIn(work, 'LOOSE chat')).toBeNull()
    expect(rowIn(work, 'GONE-folder chat')).toBeNull()
    const order = [...body.querySelectorAll('[data-session-row]')].map(el => el.textContent ?? '')
    const at = (t: string) => order.findIndex(x => x.includes(t))
    expect(at('SUB chat')).toBeLessThan(at('WORK chat'))
    expect(at('WORK chat')).toBeLessThan(at('LOOSE chat'))
    expect(at('WORK chat')).toBeLessThan(at('GONE-folder chat'))
    // The crew group's own count still counts every chat.
    expect(screen.getByTestId('crew-group-toggle-inst-a')).toHaveTextContent('4')
  })

  it('collapses one space on its own and remembers it', async () => {
    renderSidebar()
    const toggle = await screen.findByTestId('crew-space-toggle-inst-a-f-work', {}, CHAIN_WAIT)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    const work = screen.getByTestId('crew-space-inst-a-f-work')
    expect(within(work).getByText('WORK chat').closest('[aria-hidden="true"]')).not.toBeNull()
    // The crew group and the unfiled rows stay open.
    expect(screen.getByTestId('crew-group-toggle-inst-a')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('LOOSE chat').closest('[aria-hidden="true"]')).toBeNull()
    expect(JSON.parse(localStorage.getItem('mc-sidebar-crew-space-collapsed') ?? '[]')).toEqual([JSON.stringify(['inst-a', 'f-work'])])
  })

  it('lists the chats flat when the crew\'s folder read fails', async () => {
    crewPeerGetMock.mockReset().mockRejectedValue(new Error('peer down'))
    renderSidebar()
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'SUB chat')).not.toBeNull(), CHAIN_WAIT)
    expect(rowIn(group, 'WORK chat')).not.toBeNull()
    expect(screen.queryByTestId(/^crew-space-/)).toBeNull()
    // The failure is said, beside the rows it left unfiled.
    const notice = within(group).getByTestId('crew-group-spaces-error-inst-a')
    expect(notice).toHaveTextContent('peer down')
    expect(notice).toHaveTextContent(S.crew_spaces_unavailable.replace('{{name}}', 'astro'))
  })
})

describe('ChatSidebar – what a crew group header says needs you', () => {
  // The instances query, then the crew's slots: bounded so a loaded runner fails
  // on the chain, not on the default deadline.
  const CHAIN_WAIT = { timeout: 5_000 }
  const peerRow = (key: string, title: string, extra: Record<string, unknown> = {}) => ({
    ...PEER_ROW, key, row_identity: `inst-a:${key}`, title, ...extra,
  })
  const count = (template: string, n: number) => template.replace('{{count}}', String(n))
  const chats = (n: number) => count(n === 1 ? S.crew_group_chats_one : S.crew_group_chats_other, n)
  const approvals = (n: number) => count(n === 1 ? S.crew_attention_approval_one : S.crew_attention_approval_other, n)
  const questions = (n: number) => count(n === 1 ? S.crew_attention_question_one : S.crew_attention_question_other, n)
  const working = (n: number) => count(n === 1 ? S.crew_attention_working_one : S.crew_attention_working_other, n)
  const counts = (...parts: string[]) => new Intl.ListFormat('en', { type: 'unit' }).format(parts)
  // The button's name: where it goes, then the counts it shows.
  const nameOf = (target: string, list: string) => S.crew_attention_name.replace('{{target}}', target).replace('{{counts}}', list)
  const attentionOf = () => screen.queryByTestId('crew-group-attention-inst-a')
  const targetOf = () => screen.getByTestId('crew-group-attention-target-inst-a').textContent
  // Newest first: the group draws its rows in the lane's date order.
  const MIXED = [
    peerRow('p-run', 'RUN chat', { running: true, last_turn_ts: ago(10_000) }),
    peerRow('p-approve', 'APPROVE chat', { running: true, pending_approval: true, last_turn_ts: ago(20_000) }),
    peerRow('p-ask', 'ASK chat', { running: true, needs_input: true, last_turn_ts: ago(30_000) }),
    peerRow('p-run2', 'RUN2 chat', { running: true, last_turn_ts: ago(40_000) }),
    peerRow('p-idle', 'IDLE chat', { last_turn_ts: ago(50_000) }),
  ]

  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    chatFoldersMock.mockReset().mockResolvedValue([])
    listInstancesMock.mockReset().mockResolvedValue({ instances: [crew('connected')] })
    instanceChatSlotsMock.mockReset().mockResolvedValue([PEER_ROW])
    crewPeerGetMock.mockReset().mockResolvedValue([])
  })

  it('names its count as chats, and draws no attention button when nothing asks', async () => {
    renderSidebar({ relay: true })
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'REMOTE peer row')).not.toBeNull(), CHAIN_WAIT)
    expect(screen.getByTestId('crew-group-count-inst-a')).toHaveTextContent(chats(2))
    expect(attentionOf()).toBeNull()
  })

  it('counts each row once, approval over question over working, while collapsed', async () => {
    instanceChatSlotsMock.mockResolvedValue(MIXED)
    renderSidebar()
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'IDLE chat')).not.toBeNull(), CHAIN_WAIT)
    fireEvent.click(screen.getByTestId('crew-group-toggle-inst-a'))
    expect(screen.getByTestId('crew-group-toggle-inst-a')).toHaveAttribute('aria-expanded', 'false')

    expect(screen.getByTestId('crew-group-count-inst-a')).toHaveTextContent(chats(5))
    const button = attentionOf()!
    // The approval row is running and the question row is running: neither is
    // counted as working too.
    expect(within(button).getByTestId('crew-group-attention-approval-inst-a')).toHaveTextContent(approvals(1))
    expect(within(button).getByTestId('crew-group-attention-question-inst-a')).toHaveTextContent(questions(1))
    expect(within(button).getByTestId('crew-group-attention-working-inst-a')).toHaveTextContent(working(2))
    expect([...button.querySelectorAll('[data-testid^="crew-group-attention-"]:not([data-testid^="crew-group-attention-target-"])')]
      .map(el => el.textContent))
      .toEqual([approvals(1), questions(1), working(2)])
    // The visible text leads with the chat it opens, so the button reads as
    // opening that chat rather than as answering the approval it counts.
    expect(targetOf()).toBe(S.crew_attention_go_approval)
    expect(button.textContent).toBe(S.crew_attention_go_approval + approvals(1) + questions(1) + working(2))
    // The name is that same text: the target, then the counts.
    expect(button).toHaveAccessibleName(nameOf(S.crew_attention_go_approval, counts(approvals(1), questions(1), working(2))))
  })

  it('opens the first approval in draw order, above a newer working chat, and expands the group', async () => {
    instanceChatSlotsMock.mockResolvedValue([
      ...MIXED,
      peerRow('p-approve-old', 'OLDER APPROVE chat', { pending_approval: true, last_turn_ts: ago(60_000) }),
    ])
    const onOpenPeerSession = vi.fn()
    const onSelectSlot = vi.fn()
    renderSidebar({ onOpenPeerSession, onSelectSlot })
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'IDLE chat')).not.toBeNull(), CHAIN_WAIT)
    const toggle = screen.getByTestId('crew-group-toggle-inst-a')
    fireEvent.click(toggle)
    expect(JSON.parse(localStorage.getItem('mc-sidebar-crew-collapsed') ?? '[]')).toEqual(['inst-a'])

    fireEvent.click(attentionOf()!)
    expect(onOpenPeerSession).toHaveBeenCalledTimes(1)
    expect(onOpenPeerSession).toHaveBeenCalledWith('inst-a', 'p-approve')
    expect(onSelectSlot).not.toHaveBeenCalled()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(JSON.parse(localStorage.getItem('mc-sidebar-crew-collapsed') ?? '[]')).toEqual([])
  })

  it('with no approval, opens the first question before any working chat', async () => {
    instanceChatSlotsMock.mockResolvedValue([
      peerRow('p-run', 'RUN chat', { running: true, last_turn_ts: ago(10_000) }),
      peerRow('p-ask', 'ASK chat', { needs_input: true, last_turn_ts: ago(20_000) }),
      peerRow('p-ask-old', 'OLDER ASK chat', { needs_input: true, last_turn_ts: ago(30_000) }),
    ])
    const onOpenPeerSession = vi.fn()
    renderSidebar({ onOpenPeerSession })
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'OLDER ASK chat')).not.toBeNull(), CHAIN_WAIT)
    const button = attentionOf()!
    expect(targetOf()).toBe(S.crew_attention_go_question)
    expect(button.textContent).toBe(S.crew_attention_go_question + questions(2) + working(1))
    expect(button).toHaveAccessibleName(nameOf(S.crew_attention_go_question, counts(questions(2), working(1))))
    fireEvent.click(button)
    expect(onOpenPeerSession).toHaveBeenCalledWith('inst-a', 'p-ask')
  })

  it('opens only a working chat when that is all the crew has', async () => {
    instanceChatSlotsMock.mockResolvedValue([
      peerRow('p-idle', 'IDLE chat', { last_turn_ts: ago(10_000) }),
      peerRow('p-run', 'RUN chat', { running: true, last_turn_ts: ago(20_000) }),
    ])
    const onOpenPeerSession = vi.fn()
    renderSidebar({ onOpenPeerSession })
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'RUN chat')).not.toBeNull(), CHAIN_WAIT)
    const button = attentionOf()!
    expect(targetOf()).toBe(S.crew_attention_go_working)
    expect(button.textContent).toBe(S.crew_attention_go_working + working(1))
    expect(button).toHaveAccessibleName(nameOf(S.crew_attention_go_working, counts(working(1))))
    fireEvent.click(button)
    expect(onOpenPeerSession).toHaveBeenCalledWith('inst-a', 'p-run')
  })

  it('opens a local slot that runs on the crew the way its own click does', async () => {
    instanceChatSlotsMock.mockResolvedValue([peerRow('p-run', 'RUN chat', { running: true })])
    const onOpenPeerSession = vi.fn()
    const onSelectSlot = vi.fn()
    renderSidebar({ relay: true, relayExtra: { pending_approval: true }, onOpenPeerSession, onSelectSlot })
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'RUN chat')).not.toBeNull(), CHAIN_WAIT)
    fireEvent.click(attentionOf()!)
    expect(onSelectSlot).toHaveBeenCalledWith('s-relay')
    expect(onOpenPeerSession).not.toHaveBeenCalled()
  })

  it('moves the count and the read-out together under a filter', async () => {
    instanceChatSlotsMock.mockResolvedValue([
      peerRow('p-a', 'ALPHA chat', { pending_approval: true, last_turn_ts: ago(10_000) }),
      peerRow('p-b', 'BETA chat', { needs_input: true, last_turn_ts: ago(20_000) }),
      peerRow('p-g', 'GAMMA chat', { running: true, last_turn_ts: ago(30_000) }),
    ])
    renderSidebar()
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'GAMMA chat')).not.toBeNull(), CHAIN_WAIT)
    expect(screen.getByTestId('crew-group-count-inst-a')).toHaveTextContent(chats(3))
    expect(attentionOf()).toHaveAccessibleName(nameOf(S.crew_attention_go_approval, counts(approvals(1), questions(1), working(1))))

    fireEvent.change(screen.getByPlaceholderText(S.search_sessions), { target: { value: 'GAMMA' } })
    await waitFor(() => expect(rowIn(group, 'ALPHA chat')).toBeNull())
    expect(screen.getByTestId('crew-group-count-inst-a')).toHaveTextContent(chats(1))
    expect(attentionOf()).toHaveAccessibleName(nameOf(S.crew_attention_go_working, counts(working(1))))
    expect(targetOf()).toBe(S.crew_attention_go_working)
    expect(screen.queryByTestId('crew-group-attention-approval-inst-a')).toBeNull()
    expect(screen.queryByTestId('crew-group-attention-question-inst-a')).toBeNull()
  })

  it('drops the read-out with the count when the crew disconnects', async () => {
    instanceChatSlotsMock.mockResolvedValue(MIXED)
    const { qc } = renderSidebar()
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'IDLE chat')).not.toBeNull(), CHAIN_WAIT)
    expect(attentionOf()).not.toBeNull()

    listInstancesMock.mockResolvedValue({ instances: [crew('disconnected')] })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['instances'] }) })
    await waitFor(() => expect(screen.getByTestId('crew-group-inst-a')).toHaveAttribute('data-offline'))
    expect(attentionOf()).toBeNull()
    expect(screen.queryByTestId('crew-group-count-inst-a')).toBeNull()
  })

  it('keeps the header a toggle that only toggles, with the attention button beside it', async () => {
    instanceChatSlotsMock.mockResolvedValue(MIXED)
    const onOpenPeerSession = vi.fn()
    const onSelectSlot = vi.fn()
    const { store } = renderSidebar({ onOpenPeerSession, onSelectSlot })
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'IDLE chat')).not.toBeNull(), CHAIN_WAIT)
    const toggle = screen.getByTestId('crew-group-toggle-inst-a')
    const button = attentionOf()!
    // Two buttons, side by side: neither holds the other.
    expect(toggle.contains(button)).toBe(false)
    expect(toggle.querySelector('button')).toBeNull()
    expect(button.querySelector('button')).toBeNull()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(onOpenPeerSession).not.toHaveBeenCalled()
    expect(onSelectSlot).not.toHaveBeenCalled()
    expect(store.getState().chat.activeSlot).toBe('s-local')
    expect(attentionOf()).toBe(button)
  })

  it('shows a peer row\'s open question the way a local row shows one', async () => {
    instanceChatSlotsMock.mockResolvedValue([peerRow('p-ask', 'ASK chat', { needs_input: true })])
    renderSidebar()
    const group = await screen.findByTestId('crew-group-inst-a', {}, CHAIN_WAIT)
    await waitFor(() => expect(rowIn(group, 'ASK chat')).not.toBeNull(), CHAIN_WAIT)
    const row = within(group).getByText('ASK chat').closest('[data-session-row]') as HTMLElement
    expect(within(row).getByText(S.needs_your_answer)).toBeInTheDocument()
    expect(within(row).queryByTestId('session-peer-not-open-here')).toBeNull()
  })
})
