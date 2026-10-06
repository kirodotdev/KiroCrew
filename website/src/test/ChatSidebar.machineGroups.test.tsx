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
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { requestSlotReveal } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'
import { PREVIEW_INSTANCE_SESSIONS } from '../utils/previewFlags'
import en from '../i18n/locales/en.json'
import enManual from '../i18n/locales/en.manual.json'

const { instanceChatSlotsMock, listInstancesMock, chatFoldersMock } = vi.hoisted(() => ({
  instanceChatSlotsMock: vi.fn(),
  listInstancesMock: vi.fn(),
  chatFoldersMock: vi.fn(),
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

const crew = (state: string) => ({ id: 'inst-a', name: 'astro', status: { state } })
const PEER_ROW = {
  key: 'chat-9', row_identity: 'inst-a:chat-9', title: 'REMOTE peer row',
  last_turn_ts: new Date(Date.now() - 120_000).toISOString(), agent: 'default',
}

const liveSlot = (key: string, title: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '',
  last_turn_ts: new Date(Date.now() - 60_000).toISOString(), ...extra,
} as ChatSlot)

function renderSidebar({ activeSlot = 's-local', relay = false, relayFolder, relayParent, onSelectSlot }: {
  activeSlot?: string
  /** Adds a LOCAL slot whose turns run on `inst-a` (`executor: 'remote'`). */
  relay?: boolean
  /** Files that relay slot in a folder. */
  relayFolder?: string
  /** Makes the plain local slot the relay slot's creator, so the conductor lane exists. */
  relayParent?: boolean
  onSelectSlot?: (key: string) => void
} = {}) {
  const slots = [
    liveSlot('s-local', 'LOCAL plain slot'),
    ...(relay ? [liveSlot('s-relay', 'RELAY slot', {
      executor: 'remote', instance_id: 'inst-a', ...(relayFolder ? { folder_id: relayFolder } : {}),
      ...(relayParent ? { parent: { slot: 's-local', key: 's-local' } } : {}),
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
              historyHasMore={false} defaultAgent={'default'} installedAgents={[]} onSelectSlot={onSelectSlot} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store, qc }
}

const rowIn = (scope: HTMLElement, title: string) => within(scope).queryByText(title)

describe('ChatSidebar – per-machine groups', () => {
  beforeEach(() => {
    localStorage.clear()
    chatFoldersMock.mockReset().mockResolvedValue([])
    instanceChatSlotsMock.mockReset().mockResolvedValue([PEER_ROW])
    listInstancesMock.mockReset().mockResolvedValue({ instances: [crew('connected')] })
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
