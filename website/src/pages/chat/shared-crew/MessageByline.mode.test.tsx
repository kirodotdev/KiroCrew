import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { ALL_UNAVAILABLE } from '../../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewMode, useSharedCrewPeople, useSharedCrewViewer } from '../../../hooks/sharedCrew/useSharedCrew'
import { MessageByline } from './MessageByline'

vi.mock('../../../api/client', () => ({
  api: { sharedCrewViewer: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn() },
}))
const viewer = vi.mocked(api.sharedCrewViewer)
const caps = vi.mocked(api.sharedCrewCapabilities)
const modeApi = vi.mocked(api.sharedCrewMode)
const people = vi.mocked(api.sharedCrewPeople)

const me = { id: 'p_me', kind: 'human' as const, display: { name: 'Me', login: '', captured_at: null } }
// A channel-bridged message: stamped with the Unknown principal server-side.
const BRIDGED = { author: { id: 'p_unknown', kind: 'unknown' as const, display: { name: '', login: '', captured_at: null } } }

function modeBody(mode: CrewModeResponse['mode']): CrewModeResponse {
  return {
    schema_version: 1, mode, crew_id: 'crew-a', viewer: me,
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, systems_manager_attribution: 'unavailable', workload_identity_ref: 'unavailable',
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
  caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
  people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [] })
})

describe('MessageByline follows the Crew mode', () => {
  it('shows no byline in a personal Crew, even on an Unknown-stamped channel message', async () => {
    modeApi.mockResolvedValue(modeBody('personal'))
    mount()
    await screen.findByTestId('settled')
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it('shows no byline while the mode is still unknown', async () => {
    modeApi.mockReturnValue(new Promise(() => {}))
    mount(false)
    await screen.findByTestId('settled')
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it('shows no byline in a Crew that cannot attribute', async () => {
    caps.mockResolvedValue(ALL_UNAVAILABLE)
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    await screen.findByTestId('settled')
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it('shows no byline when the mode read fails, even with a setup path available', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', setup_paths: {
      tailscale: { state: 'available', reason: '' },
      kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
      systems_manager: { state: 'unavailable', reason: 'not_in_this_build' },
    } })
    modeApi.mockRejectedValue(new Error('mode unreadable'))
    mount(false)
    await screen.findByTestId('settled')
    await waitFor(() => expect(modeApi).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByTestId('mode-failed')).toBeTruthy())
    expect(screen.queryByTestId('message-byline')).toBeNull()
  })

  it.each(['network', 'console', 'invalid'] as const)('labels the Unknown sender in a %s Crew', async mode => {
    modeApi.mockResolvedValue(modeBody(mode))
    mount()
    expect((await screen.findByTestId('message-byline')).textContent).toBe('Unattributed')
  })
})
