/** Every Shared Crew query consumer belongs in this matrix, including consumers
 *  whose error is owned by another surface. Cached subtrees survive failed reads. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { ALL_UNAVAILABLE } from '../api/sharedCrewUi'
import type { CrewModeResponse } from '../api/sharedCrewMode'
import type { TailscaleInvitesStatus } from '../api/sharedCrewInvites'
import { sharedCrewKeys as keys } from '../hooks/sharedCrew/sharedCrewSocket'
import { PeoplePanel } from '../pages/settings/PeoplePanel'
import { TailscaleInvites, tailscaleInvitesKey } from '../pages/settings/TailscaleInvites'
import { TailscaleAccess, sharedCrewAccessKey } from '../pages/settings/TailscaleAccess'
import { CreatorFilterBar, CreatorFilteredSessions, useSharedCrewListView } from '../pages/chat-sidebar/CreatorFilter'
import { SessionCollabStrip } from '../pages/chat/shared-crew/SessionCollabStrip'
import { MessageByline } from '../pages/chat/shared-crew/MessageByline'
import { QueuedSendLabel } from '../pages/chat/shared-crew/QueuedSendLabel'
import { useComposerTypingLock } from '../pages/chat/shared-crew/useComposerTypingLock'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import MobileConnectModal from './MobileConnectModal'

vi.mock('../api/client', () => ({ api: {
  sharedCrewCapabilities: vi.fn(), sharedCrewPeople: vi.fn(), sharedCrewMode: vi.fn(),
  sharedCrewViewer: vi.fn(), sharedCrewPresence: vi.fn(), sharedCrewDecisions: vi.fn(),
  sharedCrewSendState: vi.fn(), sharedCrewTypingLease: vi.fn(), sharedCrewWork: vi.fn(),
  sharedCrewCapacity: vi.fn(), sharedCrewSessions: vi.fn(), sharedCrewAccess: vi.fn(),
  tailscaleInvites: vi.fn(), tailscaleUploadAvatar: vi.fn(), mobileConnectMethods: vi.fn(),
} }))

const person = (id: string) => ({ id, kind: 'human' as const, display: { name: id, login: '', captured_at: null } })
const capabilities = { ...ALL_UNAVAILABLE, attribution: 'available' as const, presence: 'available' as const, queueing: 'available' as const,
  preview: true,
  tailnet_direct: { state: 'active' as const, reason: '', method: 'cli', owner_login: 'owner@example.test', urls: ['http://crew.tail123.ts.net:5476/'], addresses: ['100.64.0.1'] },
}
const mode: CrewModeResponse = { schema_version: 1, mode: 'network', crew_id: 'crew-failure-test', viewer: person('p_me'),
  team_config: { state: 'not_configured', reason: '', capability: '', cause: '' }, console: { state: 'not_offered', url: '' }, adapters: {},
  systems_manager_attribution: 'unavailable', workload_identity_ref: 'unavailable',
}
const people = { schema_version: 1, directory: { state: 'unavailable' as const }, people: [{ principal: person('p_other'), sources: ['observed' as const], is_viewer: false }] }
const invites: TailscaleInvitesStatus = { connected: true, tailnet: 'example.test', key_expires: '', key_expired: false, key_expires_soon: false,
  admin_share_url: '', admin_keys_url: '', members: [], last_sync_at: 0, sync: {},
  invites: [{ id: 'pending', kind: 'email', email: 'pending@example.test', multi_use: false, alias: '', avatar_id: '', created_at: 0, status: 'pending', accepted_login: '' }],
}
const presence = { schema_version: 1, session_key: 's1', entries: [{ principal: person('p_other'), session_key: 's1', activity: 'typing', connections: 1, expires_at: 0 }] }
const decisions = { schema_version: 1, session_key: 's1', items: [{ decision_id: 'd1', kind: 'approval', session_key: 's1', state: 'pending', revision: 1, initiator: person('p_other'), responder: null, outcome: '', resolved_at: null }] }
const lease = { schema_version: 1, session_key: 's1', holder: person('p_other'), revision: 1, expires_at: 0 }
const queued = { work_id: 'w1', session_key: 's1', operation: 'chat_turn', state: 'queued', revision: 1, accepted_at: 0, expires_at: 0, deadline_at: null, reason: null }
const page = { schema_version: 1, items: [{ key: 'one', title: 'Saved session' }], total_matching: 1, hidden_by_filter: 0, next_cursor: '' }
const reads = [
  { method: 'sharedCrewCapabilities', key: keys.capabilities, data: capabilities },
  { method: 'sharedCrewMode', key: keys.mode, data: mode },
  { method: 'sharedCrewPeople', key: keys.people, data: people },
  { method: 'sharedCrewViewer', key: keys.viewer, data: person('p_me') },
  { method: 'sharedCrewPresence', key: keys.presence('s1'), data: presence },
  { method: 'sharedCrewDecisions', key: keys.decisions('s1'), data: decisions },
  { method: 'sharedCrewSendState', key: keys.sendState('s1'), data: { schema_version: 1, session_key: 's1', revision: 1, last: null } },
  { method: 'sharedCrewTypingLease', key: keys.typingLease('s1'), data: lease },
] as const
const clients: QueryClient[] = []
function client() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  clients.push(qc)
  for (const read of reads) qc.setQueryData(read.key, read.data)
  qc.setQueryData(tailscaleInvitesKey, invites)
  qc.setQueryData(sharedCrewAccessKey, null)
  return qc
}
function mount(ui: React.ReactNode, qc = client()) {
  const store = configureStore({ reducer: { chat: chatReducer, dashboard: dashboardReducer } })
  return { qc, ...render(<Provider store={store}><QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider></Provider>) }
}
async function refresh(qc: QueryClient, key: readonly unknown[]) {
  await act(async () => { await qc.invalidateQueries({ queryKey: key, exact: true }) })
}
beforeEach(() => {
  vi.resetAllMocks()
  for (const read of reads) vi.mocked(api[read.method]).mockResolvedValue(read.data as never)
  vi.mocked(api.tailscaleInvites).mockResolvedValue(invites)
  vi.mocked(api.sharedCrewAccess).mockResolvedValue(null)
  vi.mocked(api.mobileConnectMethods).mockResolvedValue({ methods: [] })
  vi.mocked(api.tailscaleUploadAvatar).mockResolvedValue({ avatar_id: 'picture' })
  vi.mocked(api.sharedCrewWork).mockResolvedValue({ items: [queued] } as never)
  vi.mocked(api.sharedCrewCapacity).mockResolvedValue(null)
  vi.mocked(api.sharedCrewSessions).mockResolvedValue(page)
})
afterEach(() => { for (const qc of clients.splice(0)) qc.clear() })

async function enterInviteDraft() {
  // A network Crew keeps the invite form in the share card's Invite... dialog.
  await waitFor(() => expect(screen.queryByTestId('invite-form') ?? screen.queryByTestId('share-hero-invite')).not.toBeNull())
  const opener = screen.queryByTestId('share-hero-invite')
  if (opener && !screen.queryByTestId('invite-form')) fireEvent.click(opener)
  const form = await screen.findByTestId('invite-form')
  fireEvent.change(screen.getByTestId('invite-emails'), { target: { value: 'draft@example.test' } })
  fireEvent.change(screen.getByTestId('invite-alias'), { target: { value: 'Draft name' } })
  fireEvent.change(screen.getByTestId('invite-avatar-input'), { target: { files: [new File(['png'], 'picture.png', { type: 'image/png' })] } })
  await screen.findByTestId('invite-avatar-preview')
  return form
}
function expectDraft(form: HTMLElement) {
  expect(screen.getByTestId('invite-form')).toBe(form)
  expect(screen.getByTestId('invite-emails')).toHaveValue('draft@example.test')
  expect(screen.getByTestId('invite-alias')).toHaveValue('Draft name')
  expect(screen.getByTestId('invite-avatar-preview')).toHaveAttribute('src', expect.stringContaining('picture'))
}

describe('Shared Crew cached forms', () => {
  it.each(reads.slice(0, 3))('People keeps the invite draft when $method fails and recovers', async read => {
    const { qc } = mount(<PeoplePanel />)
    const form = await enterInviteDraft()
    vi.mocked(api[read.method]).mockRejectedValue(new Error('read failed'))
    await refresh(qc, read.key)
    const error = await screen.findByRole('alert')
    expect(error).toHaveTextContent('read failed')
    expect(within(error).queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    expectDraft(form)
    vi.mocked(api[read.method]).mockResolvedValue(read.data as never)
    await refresh(qc, read.key)
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expectDraft(form)
  })

  it.each([true, false])('keeps the %s-connected invite form on a failed status poll', async connected => {
    const qc = client()
    qc.setQueryData(tailscaleInvitesKey, { ...invites, connected })
    mount(<TailscaleInvites />, qc)
    const form = connected ? await enterInviteDraft() : await screen.findByTestId('invite-key-input')
    if (!connected) fireEvent.change(form, { target: { value: 'tskey-api-unsaved-draft' } })
    vi.mocked(api.tailscaleInvites).mockRejectedValue(new ApiError(500, 'status failed', '{}'))
    await refresh(qc, tailscaleInvitesKey)
    const error = await screen.findByTestId('invite-load-error')
    expect(within(error).queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    if (connected) expectDraft(form)
    else expect(screen.getByTestId('invite-key-input')).toBe(form)
    vi.mocked(api.tailscaleInvites).mockResolvedValue({ ...invites, connected })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByTestId('invite-load-error')).toBeNull())
    if (connected) expectDraft(form)
    else expect(form).toHaveValue('tskey-api-unsaved-draft')
  })

  it('keeps the access rows and open removal confirmation through failure and recovery', async () => {
    const qc = client()
    const view = { readable: true, local_authorization: true, waiting: [], approved: [{ user_id: 'a', login: 'a@example.test', display_name: 'Person', avatar_url: '', first_seen: 0, approved_at: 0, approved_by: 'owner' }], seeded: [], first_share_ack: true, first_share: null }
    qc.setQueryData(sharedCrewAccessKey, view)
    mount(<TailscaleAccess />, qc)
    fireEvent.click(await screen.findByTestId('approved-remove'))
    const dialog = await screen.findByRole('dialog')
    const row = screen.getByTestId('approved-a')
    vi.mocked(api.sharedCrewAccess).mockRejectedValue(new Error('access failed'))
    await refresh(qc, sharedCrewAccessKey)
    const error = await screen.findByTestId('access-load-error')
    expect(within(error).queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    expect(screen.getByTestId('approved-a')).toBe(row)
    expect(screen.getByRole('dialog')).toBe(dialog)
    vi.mocked(api.sharedCrewAccess).mockResolvedValue(view)
    await refresh(qc, sharedCrewAccessKey)
    await waitFor(() => expect(screen.queryByTestId('access-load-error')).toBeNull())
    expect(screen.getByRole('dialog')).toBe(dialog)
  })
})

function Sidebar() {
  const view = useSharedCrewListView()
  return <>{view.filterAvailable && <CreatorFilterBar value={view.filter} onChange={view.setFilter} />}{view.filter !== 'all' && <CreatorFilteredSessions creator={view.filter} q="" connected onOpen={() => {}} onClear={() => view.setFilter('all')} />}</>
}
function Lock() {
  const lock = useComposerTypingLock('s1')
  return <span data-testid="lock">{lock?.holderName}</span>
}
function Chat() {
  return <><SessionCollabStrip sessionKey="s1" /><Lock />{[1, 2, 3].map(id => <MessageByline key={id} meta={{ author: person('p_other') }} />)}<MessageByline meta={{ author: person('p_me') }} /></>
}

describe('Shared Crew failure owners', () => {
  it.each(reads.slice(0, 3))('sidebar reports an initial $method failure with no open chat', async read => {
    const qc = client()
    qc.removeQueries({ queryKey: read.key, exact: true })
    vi.mocked(api[read.method]).mockRejectedValue(new Error('sidebar failed'))
    mount(<Sidebar />, qc)
    expect(await screen.findByRole('alert')).toHaveTextContent('sidebar failed')
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(api[read.method]).toHaveBeenCalledTimes(1)
    vi.mocked(api[read.method]).mockResolvedValue(read.data as never)
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(await screen.findByTestId('creator-filter')).toBeInTheDocument()
  })

  it.each(reads.slice(0, 3))('sidebar retains the filter and filtered page when $method fails', async read => {
    window.localStorage.setItem('kc.shared-crew.creator-filter.crew-failure-test', 'mine')
    const { qc } = mount(<Sidebar />)
    const row = await screen.findByText('Saved session')
    const select = screen.getByTestId('creator-filter')
    vi.mocked(api[read.method]).mockRejectedValue(new Error('sidebar failed'))
    await refresh(qc, read.key)
    expect(await screen.findByRole('alert')).toHaveTextContent('sidebar failed')
    expect(screen.getByTestId('creator-filter')).toBe(select)
    expect(screen.getByText('Saved session')).toBe(row)
    vi.mocked(api[read.method]).mockResolvedValue(read.data as never)
    await refresh(qc, read.key)
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(screen.getByText('Saved session')).toBe(row)
  })

  it.each(reads)('chat reports $method once and keeps cached attribution and typing state', async read => {
    const { qc } = mount(<Chat />)
    const strip = await screen.findByTestId('session-collab-strip')
    expect(screen.getAllByTestId('message-byline')).toHaveLength(3)
    expect(screen.getByTestId('lock')).toHaveTextContent('p_other')
    vi.mocked(api[read.method]).mockRejectedValue(new Error('chat read failed'))
    await refresh(qc, read.key)
    expect(await screen.findByRole('alert')).toHaveTextContent('chat read failed')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByTestId('session-collab-strip')).toBe(strip)
    expect(screen.getAllByTestId('message-byline')).toHaveLength(3)
    expect(screen.getByTestId('lock')).toHaveTextContent('p_other')
    vi.mocked(api[read.method]).mockResolvedValue(read.data as never)
    await refresh(qc, read.key)
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(screen.getByTestId('session-collab-strip')).toBe(strip)
  })

  it.each(reads)('chat reports an initial $method failure without a sidebar', async read => {
    const qc = client()
    qc.removeQueries({ queryKey: read.key, exact: true })
    vi.mocked(api[read.method]).mockRejectedValue(new Error('initial chat read failed'))
    mount(<SessionCollabStrip sessionKey="s1" />, qc)
    expect(await screen.findByRole('alert')).toHaveTextContent('initial chat read failed')
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('reports a work refetch failure beside the cached queued send', async () => {
    const { qc } = mount(<QueuedSendLabel slotKey="s1" />)
    const cancel = await screen.findByRole('button', { name: 'Cancel this queued message' })
    vi.mocked(api.sharedCrewWork).mockRejectedValue(new Error('work failed'))
    await refresh(qc, keys.work('s1'))
    expect(await screen.findByTestId('queued-send-read-error')).toHaveTextContent('work failed')
    expect(screen.getByRole('button', { name: 'Cancel this queued message' })).toBe(cancel)
  })

  it('reports capacity failure even when no work is queued', async () => {
    vi.mocked(api.sharedCrewWork).mockResolvedValue({ items: [] })
    vi.mocked(api.sharedCrewCapacity).mockRejectedValue(new Error('capacity failed'))
    mount(<QueuedSendLabel slotKey="s1" />)
    expect(await screen.findByTestId('queued-send-capacity-error')).toHaveTextContent('capacity failed')
  })

  it('keeps a cached session page on a failed refetch', async () => {
    const { qc } = mount(<CreatorFilteredSessions creator="mine" q="" connected onOpen={() => {}} onClear={() => {}} />)
    const row = await screen.findByText('Saved session')
    vi.mocked(api.sharedCrewSessions).mockRejectedValue(new Error('page failed'))
    await refresh(qc, [...keys.sessions('mine', ''), ''])
    expect(await screen.findByTestId('creator-filter-error')).toHaveTextContent('page failed')
    expect(screen.getByText('Saved session')).toBe(row)
  })

  it('phone dialog reports capabilities failure outside chat and retries', async () => {
    const qc = client()
    qc.removeQueries({ queryKey: keys.capabilities })
    vi.mocked(api.sharedCrewCapabilities).mockRejectedValue(new ApiError(500, 'capabilities failed', '{}'))
    mount(<MobileConnectModal kinds={['login_link']} onClose={() => {}} />, qc)
    expect(await screen.findByTestId('mobile-connect-capabilities-error')).toHaveTextContent('capabilities failed')
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create sign-in link' })).toBeInTheDocument()
    vi.mocked(api.sharedCrewCapabilities).mockResolvedValue(capabilities)
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByTestId('tailnet-direct-link')).toBeInTheDocument()
    expect(screen.queryByTestId('mobile-connect-capabilities-error')).toBeNull()
  })

  it('phone dialog keeps the cached direct link and open Advanced fold on failure', async () => {
    const { qc } = mount(<MobileConnectModal kinds={['login_link']} onClose={() => {}} />)
    const link = await screen.findByTestId('tailnet-direct-link')
    const advanced = screen.getByTestId('mobile-connect-advanced')
    advanced.setAttribute('open', '')
    vi.mocked(api.sharedCrewCapabilities).mockRejectedValue(new Error('capabilities failed'))
    await refresh(qc, keys.capabilities)
    expect(await screen.findByRole('alert')).toHaveTextContent('capabilities failed')
    expect(screen.getByTestId('tailnet-direct-link')).toBe(link)
    expect(screen.getByTestId('mobile-connect-advanced')).toBe(advanced)
    expect(advanced).toHaveAttribute('open')
  })

  it('a successful null capabilities response intentionally hides optional surfaces', async () => {
    const qc = client()
    qc.setQueryData(keys.capabilities, null)
    mount(<><Sidebar /><MobileConnectModal kinds={['login_link']} onClose={() => {}} /></>, qc)
    expect(screen.queryByTestId('creator-filter')).toBeNull()
    expect(screen.queryByTestId('tailnet-direct-link')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button', { name: 'Create sign-in link' })).toBeInTheDocument()
  })
})

describe('People read-only modes', () => {
  it.each(['console', 'invalid'] as const)('keeps the hand-off available for a cached %s surface', async currentMode => {
    const qc = client()
    qc.setQueryData(keys.mode, { ...mode, mode: currentMode })
    mount(<PeoplePanel />, qc)
    vi.mocked(api.sharedCrewPeople).mockRejectedValue(new Error('people failed'))
    await refresh(qc, keys.people)
    const error = await screen.findByTestId('people-error')
    expect(within(error).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })
})

describe('Remote Control methods read', () => {
  it.each(['personal', 'network'] as const)('reports an initial %s methods failure and retries without losing the invite draft', async currentMode => {
    const qc = client()
    qc.setQueryData(keys.mode, { ...mode, mode: currentMode })
    vi.mocked(api.mobileConnectMethods).mockRejectedValue(new ApiError(503, 'methods failed', '{}'))
    mount(<PeoplePanel />, qc)
    const form = await enterInviteDraft()
    const error = await screen.findByTestId('remote-control-error')
    expect(error).toHaveTextContent('methods failed')
    expect(within(error).queryByRole('button', { name: 'Ask the agent' })).toBeNull()
    expect(screen.queryByTestId('remote-control-unavailable')).toBeNull()
    vi.mocked(api.mobileConnectMethods).mockResolvedValue({ methods: [{ kind: 'login_link' }] } as never)
    fireEvent.click(within(screen.getByTestId('remote-control-setup')).getByRole('button', { name: 'Try again' }))
    await screen.findByRole('button', { name: 'Set up Remote Control' })
    expect(screen.queryByTestId('remote-control-error')).toBeNull()
    expectDraft(form)
  })

  it('keeps cached methods during a failed refresh, then shows unavailable only after a successful empty read', async () => {
    const qc = client()
    qc.setQueryData(['mobile-connect-methods'], { methods: [{ kind: 'login_link' }] })
    mount(<PeoplePanel />, qc)
    const form = await enterInviteDraft()
    const open = await screen.findByRole('button', { name: 'Set up Remote Control' })
    vi.mocked(api.mobileConnectMethods).mockRejectedValue(new Error('refresh failed'))
    await refresh(qc, ['mobile-connect-methods'])
    expect(await screen.findByTestId('remote-control-error')).toHaveTextContent('refresh failed')
    expect(screen.getByRole('button', { name: 'Set up Remote Control' })).toBe(open)
    expect(screen.queryByTestId('remote-control-unavailable')).toBeNull()
    expectDraft(form)
    vi.mocked(api.mobileConnectMethods).mockResolvedValue({ methods: [] })
    fireEvent.click(within(screen.getByTestId('remote-control-setup')).getByRole('button', { name: 'Try again' }))
    await screen.findByTestId('remote-control-unavailable')
    expect(screen.queryByTestId('remote-control-error')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Set up Remote Control' })).toBeNull()
    expectDraft(form)
  })
})

it('does not claim Remote Control is unavailable while methods are still loading', async () => {
  let finish!: (value: { methods: [] }) => void
  vi.mocked(api.mobileConnectMethods).mockImplementation(() => new Promise(resolve => { finish = resolve }))
  mount(<PeoplePanel />)
  const card = await screen.findByTestId('remote-control-setup')
  expect(within(card).getByRole('status')).toHaveTextContent('Checking remote access')
  expect(screen.queryByTestId('remote-control-unavailable')).toBeNull()
  await act(async () => { finish({ methods: [] }) })
  expect(await screen.findByTestId('remote-control-unavailable')).toBeInTheDocument()
})
