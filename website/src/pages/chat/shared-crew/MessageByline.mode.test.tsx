import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { ALL_UNAVAILABLE } from '../../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewMode, useSharedCrewPeople, useSharedCrewViewer } from '../../../hooks/sharedCrew/useSharedCrew'
import { AppSenderByline, MessageByline } from './MessageByline'

vi.mock('../../../api/client', () => ({
  api: { sharedCrewViewer: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn(), listApps: vi.fn() },
}))
const viewer = vi.mocked(api.sharedCrewViewer)
const caps = vi.mocked(api.sharedCrewCapabilities)
const modeApi = vi.mocked(api.sharedCrewMode)
const people = vi.mocked(api.sharedCrewPeople)
const listApps = vi.mocked(api.listApps)

const me = { id: 'p_me', kind: 'human' as const, display: { name: 'Me', login: '', captured_at: null } }
const TEAMMATE = { principal: { id: 'p_tm', kind: 'human' as const, display: { name: 'Tam', login: '', captured_at: null } }, sources: ['observed' as const], is_viewer: false }
// A channel-bridged message: stamped with the Unknown principal server-side.
const BRIDGED = { author: { id: 'p_unknown', kind: 'unknown' as const, display: { name: '', login: '', captured_at: null } } }

// The byline reads capabilities first; only then are the mode and People
// queries enabled. Waits that span that chain get this ceiling.
const CHAIN_TIMEOUT_MS = 5000

function modeBody(mode: CrewModeResponse['mode']): CrewModeResponse {
  return {
    schema_version: 1, mode, crew_id: 'crew-a', viewer: me,
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, workload_identity_ref: 'unavailable',
  }
}

/** Shares the byline's query cache and marks when every read it waits on has
 *  settled, so an absent byline is asserted after the data arrived. */
function Probe({ modeSettles }: { modeSettles: boolean }) {
  const caps = useSharedCrewCapabilities()
  const attributes = caps.capabilities.attribution === 'available'
  const mode = useSharedCrewMode(attributes)
  const people = useSharedCrewPeople(attributes)
  const viewerQuery = useSharedCrewViewer()
  const reads = attributes ? [caps.isSuccess, people.isSuccess, viewerQuery.isSuccess, !modeSettles || mode.isSuccess] : [caps.isSuccess, viewerQuery.isSuccess]
  if (!reads.every(Boolean)) return null
  return <><span data-testid="settled" />{mode.isError && <span data-testid="mode-failed" />}</>
}

function mount(modeSettles = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MessageByline meta={BRIDGED} /><Probe modeSettles={modeSettles} /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  viewer.mockResolvedValue(me)
  // The Shared Crew preview on and a teammate joined; each case varies the mode.
  caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', shared_crew_preview: true })
  people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [TEAMMATE] })
  listApps.mockResolvedValue([{ name: 'issue-radar', displayName: 'Issue Radar' }] as unknown as Awaited<ReturnType<typeof api.listApps>>)
})

describe('MessageByline follows the Crew mode', () => {
  it('shows no byline in a personal Crew, even on an Unknown-stamped channel message', async () => {
    modeApi.mockResolvedValue(modeBody('personal'))
    mount()
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it('shows no byline while the mode is still unknown', async () => {
    modeApi.mockReturnValue(new Promise(() => {}))
    mount(false)
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it('shows no byline in a Crew that cannot attribute', async () => {
    caps.mockResolvedValue(ALL_UNAVAILABLE)
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it('shows no byline when the mode read fails, even with a setup path available', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', setup_paths: {
      tailscale: { state: 'available', reason: '' },
      kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
    } })
    modeApi.mockRejectedValue(new Error('mode unreadable'))
    mount(false)
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(modeApi).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByTestId('mode-failed')).toBeTruthy())
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it('shows no byline in a network Crew with the Shared Crew preview off', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(caps).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it.each([
    ['no one besides the owner', []],
    ['only the owner', [{ ...TEAMMATE, is_owner: true }]],
    ['a session creator who never connected, such as a Slack sender', [{ ...TEAMMATE, sources: ['session_creator' as const] }]],
  ])('shows no byline in a network Crew with %s', async (_who, rows) => {
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: rows })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    await waitFor(() => expect(people).toHaveBeenCalled(), { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it.each(['personal', 'network'] as const)('names the app that sent a message in a %s Crew', async mode => {
    modeApi.mockResolvedValue(modeBody(mode))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const appAuthor = { id: 'p_app', kind: 'system' as const, display: { name: 'app:issue-radar', login: '', captured_at: null } }
    render(
      <QueryClientProvider client={qc}>
        <MessageByline meta={{ turnActor: 'app', author: appAuthor }} />
        <MessageByline meta={{ turnActor: 'app', appLabel: 'notes-app' }} />
        <MessageByline meta={{ turnActor: 'app' }} />
        <MessageByline meta={{ author: me }} />
      </QueryClientProvider>,
    )
    // The installed app's display name once the app list arrives; the id for an
    // app the list does not hold.
    await waitFor(() => expect(screen.getAllByTestId('message-app-sender').map(el => el.textContent)).toEqual([
      'Sent by the Issue Radar app',
      'Sent by the notes-app app',
      'Sent by an app',
    ]))
  })

  it('keeps the app id on a failed app list read and adds no notice to the row', async () => {
    // The composer dock reports this failure once per chat, from the same query.
    listApps.mockRejectedValue(new Error('HTTP 500 listing apps'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <AppSenderByline meta={{ turnActor: 'app', appLabel: 'issue-radar' }} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(qc.getQueryState(['apps'])?.status).toBe('error'))
    expect(screen.queryByText(/HTTP 500 listing apps/)).toBeNull()
    expect(screen.getByTestId('message-app-sender').textContent).toBe('Sent by the issue-radar app')
  })

  it('names the app on a steered answer, which skips the person byline', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <AppSenderByline meta={{ steer: true, steerState: 'consumed', turnActor: 'app', appLabel: 'issue-radar' }} />
        <AppSenderByline meta={{ steer: true, steerState: 'consumed' }} />
      </QueryClientProvider>,
    )
    expect((await screen.findByText('Sent by the Issue Radar app')).getAttribute('data-testid')).toBe('message-app-sender')
    expect(screen.getAllByTestId('message-app-sender')).toHaveLength(1)
  })

  it.each(['network', 'console', 'invalid'] as const)('labels the Unknown sender in a %s Crew', async mode => {
    modeApi.mockResolvedValue(modeBody(mode))
    mount()
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Unattributed')
  })
})
