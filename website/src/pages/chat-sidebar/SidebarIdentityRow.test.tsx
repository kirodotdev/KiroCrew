import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ALL_UNAVAILABLE, type PeopleRow } from '../../api/sharedCrewUi'
import type { CrewModeResponse, ViewerVia } from '../../api/sharedCrewMode'
import { useSharedCrewCapabilities, useSharedCrewMode, useSharedCrewPeople } from '../../hooks/sharedCrew/useSharedCrew'
import { SidebarIdentityRow, identityLabel } from './SidebarIdentityRow'

vi.mock('../../api/client', () => ({
  api: { sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn() },
}))
const caps = vi.mocked(api.sharedCrewCapabilities)
const modeApi = vi.mocked(api.sharedCrewMode)
const people = vi.mocked(api.sharedCrewPeople)

// Capabilities first, then mode and People: waits that span the chain get this ceiling.
const CHAIN_TIMEOUT_MS = 5000

const me = { id: 'p_me', kind: 'human' as const, display: { name: 'Ana', login: '', captured_at: null } }
const row = (id: string, over: Partial<PeopleRow> = {}): PeopleRow => ({
  principal: { id, kind: 'human', display: { name: id, login: '', captured_at: null } }, sources: ['observed'], ...over,
})

function modeBody(mode: CrewModeResponse['mode'], via: ViewerVia = 'kiro_login'): CrewModeResponse {
  return {
    schema_version: 1, mode, crew_id: 'crew-a', viewer: me,
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, workload_identity_ref: 'unavailable',
    viewer_via: via, viewer_is_owner: false, owner_label: 'Studio Mac',
  }
}

/** Marks when every read the row waits on has settled, so an absent row is
 *  asserted after the data arrived. */
function Probe() {
  const c = useSharedCrewCapabilities()
  const on = c.capabilities.attribution === 'available' && c.capabilities.shared_crew_preview === true
  const m = useSharedCrewMode(on)
  const p = useSharedCrewPeople(on)
  const settled = on ? m.isSuccess && p.isSuccess : c.isSuccess
  return settled ? <span data-testid="settled" /> : null
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><SidebarIdentityRow /><Probe /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', shared_crew_preview: true })
  modeApi.mockResolvedValue(modeBody('network'))
  // A relay teammate viewing, the owner's row, and one more teammate with a tab open.
  people.mockResolvedValue({
    schema_version: 1, directory: { state: 'unavailable' },
    people: [
      row('p_me', { is_viewer: true, via: 'kiro_login' }),
      row('p_owner', { is_owner: true, via: 'local', sources: ['owner'] }),
      row('p_bo', { via: 'kiro_login', live: { activity: 'viewing', session_key: '', session_title: '' } }),
    ],
  })
})

describe('SidebarIdentityRow', () => {
  it('names who the viewer is recorded as and the path, with a read-only Shared count', async () => {
    mount()
    expect(await screen.findByTestId('sidebar-identity', {}, { timeout: CHAIN_TIMEOUT_MS })).toHaveTextContent('Ana via Kiro Login')
    expect(screen.getByTestId('sidebar-identity')).toHaveAttribute('title', 'What you send here is recorded as Ana.')
    // The viewer and the teammate with a tab open; the owner has none.
    const shared = screen.getByTestId('sidebar-shared-status')
    expect(shared).toHaveTextContent('2')
    expect(shared).toHaveAttribute('aria-label', 'Shared · 2 here now')
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('shows nothing on a personal Crew', async () => {
    modeApi.mockResolvedValue(modeBody('personal', 'local'))
    mount()
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('sidebar-identity-row')).toBeNull()
  })

  it('shows nothing with the Shared Crew preview off', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    mount()
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('sidebar-identity-row')).toBeNull()
    expect(modeApi).not.toHaveBeenCalled()
  })

  it('shows nothing until someone besides the owner has joined', async () => {
    modeApi.mockResolvedValue(modeBody('network', 'local'))
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [row('p_me', { is_viewer: true, is_owner: true, via: 'local' })] })
    mount()
    await screen.findByTestId('settled', {}, { timeout: CHAIN_TIMEOUT_MS })
    expect(screen.queryByTestId('sidebar-identity-row')).toBeNull()
  })
})

describe('identityLabel', () => {
  it('names each path, and the name alone at this machine or on an unnamed path', () => {
    expect(identityLabel('Ana', 'tailscale')).toBe('Ana via Tailscale')
    expect(identityLabel('Ana', 'ssm')).toBe('Ana via Systems Manager')
    expect(identityLabel('Ana', 'local')).toBe('Ana')
    expect(identityLabel('Ana', 'unknown')).toBe('Ana')
    expect(identityLabel('Ana', undefined)).toBe('Ana')
  })
})
