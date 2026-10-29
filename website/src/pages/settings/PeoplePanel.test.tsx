import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ALL_UNAVAILABLE, type PeopleResponse } from '../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { PeoplePanel } from './PeoplePanel'

vi.mock('../../api/client', () => ({
  api: { sharedCrewPeople: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), mobileConnectMethods: vi.fn(), tailscaleInvites: vi.fn(), sharedCrewAccess: vi.fn() },
}))
const people = vi.mocked(api.sharedCrewPeople)
const caps = vi.mocked(api.sharedCrewCapabilities)
const modeApi = vi.mocked(api.sharedCrewMode)
const mobile = vi.mocked(api.mobileConnectMethods)
const invitesApi = vi.mocked(api.tailscaleInvites)

function modeBody(mode: CrewModeResponse['mode'], over: Partial<CrewModeResponse> = {}): CrewModeResponse {
  return {
    schema_version: 1, mode, crew_id: 'crew-a',
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' },
    adapters: {
      tailscale: { state: 'available', reason: '' },
      kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
      systems_manager: { state: 'unavailable', reason: 'not_in_this_build' },
    },
    systems_manager_attribution: 'unavailable', workload_identity_ref: 'unavailable', ...over,
  }
}

const row = (id: string, name: string, over: Partial<PeopleResponse['people'][number]> = {}) => ({
  principal: { id, kind: 'human' as const, display: { name, login: `${id}@example.test`, captured_at: null } },
  sources: ['observed' as const], last_seen_at: 1_700_000_000, devices: 2, sessions_created: 3,
  is_viewer: false, usage: { active: 1, queued: 2 }, ...over,
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><PeoplePanel /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  // An older gateway: no mode route, so the mode is read from capabilities.
  modeApi.mockResolvedValue(null)
  mobile.mockResolvedValue({ methods: [] })
  // Not the owner, or no invite routes: the invite card stays hidden.
  invitesApi.mockResolvedValue(null)
  vi.mocked(api.sharedCrewAccess).mockResolvedValue(null)
})

/** A Crew with the tailnet path set up, so the People list itself renders. */
const SHARED = {
  ...ALL_UNAVAILABLE,
  setup_paths: {
    tailscale: { state: 'available' as const, reason: '' as const },
    kiro_login: { state: 'unavailable' as const, reason: 'not_in_this_build' as const },
    systems_manager: { state: 'unavailable' as const, reason: 'not_in_this_build' as const },
  },
}

describe('PeoplePanel', () => {
  it('lists observed people apart from the directory, with sources and usage, read-only', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', membership_link: 'tailscale_admin' })
    people.mockResolvedValue({
      schema_version: 1, directory: { state: 'unavailable' }, usage_available: true,
      people: [row('p_a', 'Alice', { is_viewer: true }), row('p_b', 'Bob', { sources: ['session_creator'], last_seen_at: null })],
    })
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.getByText('Alice')).toBeInTheDocument()
    expect(screen.getByText('You')).toBeInTheDocument()
    expect(screen.getByText('Seen connecting')).toBeInTheDocument()
    expect(screen.getByText('Created sessions')).toBeInTheDocument()
    expect(screen.getAllByTestId('person-usage')[0].textContent).toBe('1 running · 2 waiting')
    expect(screen.getByText(/has no assigned-user directory/)).toBeInTheDocument()
    expect(screen.getByTestId('tailscale-admin-link')).toHaveAttribute('href', 'https://login.tailscale.com/admin/users')
    // Nothing on this surface manages membership.
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryByText(/add|remove|role|admin/i, { selector: 'button' })).toBeNull()
  })

  it('shows the console pointer only when the capability names it', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', membership_link: 'console' })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('console-pointer')).toBeInTheDocument()
    expect(screen.queryByTestId('tailscale-admin-link')).toBeNull()
    expect(screen.getByText(/cannot report how much work/)).toBeInTheDocument()
  })

  it('shows a capabilities read failure with agent recovery, never unavailable attribution', async () => {
    caps.mockRejectedValue(new Error('capabilities read failed'))
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('people-capabilities-error')).toHaveTextContent('capabilities read failed')
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.queryByTestId('people-unavailable')).toBeNull()
    expect(screen.queryByTestId('shared-access-setup')).toBeNull()
  })

  it('degrades when the Crew does not attribute', async () => {
    caps.mockResolvedValue({ ...SHARED })
    people.mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('people-unavailable')).toBeInTheDocument()
  })

  it('says people cannot be named where the host cannot prove who comes through Tailscale Serve', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'unavailable', reasons: { attribution: 'platform_unprovable' } })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('people-unnamed')).toBeInTheDocument()
    expect(screen.getByText('People cannot be named on this machine')).toBeInTheDocument()
    expect(screen.getByText(/this operating system cannot prove who each one is/)).toBeInTheDocument()
    expect(screen.queryByTestId('people-unavailable')).toBeNull()
  })

  it('names a same-user Tailscale as the reason', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'unavailable', reasons: { attribution: 'daemon_same_user' } })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('people-unnamed')).toBeInTheDocument()
    expect(screen.getByText(/runs as the same user as this Crew/)).toBeInTheDocument()
  })

  it('notes when Tailscale was not running at start', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'degraded', reasons: { attribution: 'daemon_not_found' } })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('people-daemon-not-found')).toBeInTheDocument()
    expect(screen.getByTestId('people-panel')).toBeInTheDocument()
  })

  it('lists teammates instead of the setup state once someone else has connected', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue({
      schema_version: 1, directory: { state: 'unavailable' }, usage_available: false,
      people: [row('p_a', 'Alice', { is_viewer: true }), row('p_b', 'Bob')],
    })
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.getByText('Bob')).toBeInTheDocument()
    expect(screen.queryByTestId('shared-access-setup')).toBeNull()
    expect(screen.queryByText(/only you can use it/)).toBeNull()
  })

  it('keeps the setup state when only the viewer has been seen', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue({
      schema_version: 1, directory: { state: 'unavailable' }, usage_available: false,
      people: [row('p_a', 'Alice', { is_viewer: true })],
    })
    mount()
    expect(await screen.findByTestId('shared-access-setup')).toBeInTheDocument()
  })

  it('shows one setup empty state when no shared-access path is configured', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('shared-access-setup')).toBeInTheDocument()
    expect(screen.queryByTestId('people-panel')).toBeNull()
    expect(screen.getByText('Set up shared team access')).toBeInTheDocument()
    expect(screen.getByTestId('setup-path-tailscale-status').textContent).toBe('Not set up')
    expect(screen.getByTestId('setup-path-kiro_login-status').textContent).toBe('Not available in this build')
    expect(screen.getByTestId('setup-path-systems_manager-status').textContent).toBe('Not available in this build')
    // Guidance only: no control that does nothing, and links only to existing guides.
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.getByTestId('setup-path-tailscale-guide')).toHaveAttribute('href', expect.stringContaining('docs/guides/remote-and-mobile.md#share-one-crew-with-teammates'))
    expect(screen.getByTestId('setup-path-systems_manager-guide')).toHaveAttribute('href', expect.stringContaining('docs/guides/cloud-instance-ssm-vs-ssh.md'))
    expect(screen.queryByTestId('setup-path-kiro_login-guide')).toBeNull()
  })

  it('shows the setup state on an older gateway without the routes', async () => {
    caps.mockResolvedValue(null)
    people.mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('shared-access-setup')).toBeInTheDocument()
  })

  it('names each path with the availability the capabilities report', async () => {
    caps.mockResolvedValue({
      ...ALL_UNAVAILABLE,
      setup_paths: {
        tailscale: { state: 'degraded', reason: 'tailnet_unreachable' },
        kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
        systems_manager: { state: 'unavailable', reason: 'not_in_this_build' },
      },
    })
    people.mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('setup-path-tailscale')).toHaveAttribute('data-state', 'degraded')
    expect(screen.getByTestId('setup-path-tailscale-status').textContent).toBe('On, but not reachable')
  })

  it('leaves the setup state once a capability source reports a path available', async () => {
    caps.mockResolvedValue({
      ...ALL_UNAVAILABLE,
      attribution: 'available',
      setup_paths: {
        tailscale: { state: 'unavailable', reason: 'not_configured' },
        kiro_login: { state: 'available', reason: '' },
        systems_manager: { state: 'unavailable', reason: 'not_in_this_build' },
      },
    })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.queryByTestId('shared-access-setup')).toBeNull()
  })
})

describe('PeoplePanel modes', () => {
  const EMPTY: PeopleResponse = { schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] }

  it('network: adapters are read-only, SSM points to IAM and Session Manager and says its work is credited to whoever runs this Crew, Remote Control shows', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.getByTestId('adapter-tailscale')).toHaveAttribute('data-state', 'available')
    expect(screen.getByTestId('adapter-tailscale-docs')).toHaveAttribute('href', 'https://tailscale.com/kb/1084/sharing')
    expect(screen.getByTestId('adapter-systems_manager').textContent).toMatch(/IAM and Session Manager/)
    expect(screen.getByTestId('adapter-systems_manager').textContent).toMatch(/credited to whoever runs this Crew/)
    expect(screen.getByTestId('adapter-systems_manager').textContent).not.toMatch(/Unattributed/)
    expect(screen.getByTestId('adapter-ssm-docs')).toHaveAttribute('href', expect.stringContaining('session-manager'))
    expect(screen.getByTestId('adapter-kiro_login').textContent).toContain('Not available in this build')
    expect(screen.getByTestId('remote-control-setup')).toBeInTheDocument()
    expect(await screen.findByTestId('remote-control-unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('people-console')).toBeNull()
    // Read-only: no invite, revoke or role control.
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('network: offers the existing phone connection when a method is renderable', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockResolvedValue(modeBody('network'))
    mobile.mockResolvedValue({ methods: [{ kind: 'tailnet_qr' }] } as never)
    mount()
    expect(await screen.findByTestId('remote-control-setup')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Set up Remote Control/ })).toBeInTheDocument()
  })

  it('console: read-only Team Access and the validated Console link, with no setup or Remote Control surface', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('console', {
      team_config: { state: 'valid', reason: '', capability: '', cause: '' },
      console: { state: 'available', url: 'https://console.example.test/crews/1' },
    }))
    mount()
    expect(await screen.findByTestId('people-console')).toBeInTheDocument()
    expect(screen.getByText('Team access')).toBeInTheDocument()
    expect(screen.getByText('Bob')).toBeInTheDocument()
    expect(screen.getByTestId('console-link')).toHaveAttribute('href', 'https://console.example.test/crews/1')
    expect(screen.queryByTestId('shared-access-setup')).toBeNull()
    expect(screen.queryByTestId('remote-control-setup')).toBeNull()
    expect(screen.queryByTestId('adapter-tailscale')).toBeNull()
    expect(screen.queryByTestId('tailscale-admin-link')).toBeNull()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('console: an unsafe link is never rendered', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockResolvedValue(modeBody('console', { console: { state: 'available', url: 'javascript:alert(1)' } }))
    mount()
    expect(await screen.findByTestId('console-link-missing')).toBeInTheDocument()
    expect(screen.queryByTestId('console-link')).toBeNull()
    expect(screen.getByText('No one has signed in yet.')).toBeInTheDocument()
  })

  it('invalid: an error state with the reason code, never the personal surface', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockResolvedValue(modeBody('invalid', {
      team_config: { state: 'invalid', reason: 'console_capability_missing', capability: 'individual_grants', cause: 'provider_refused' },
    }))
    mount()
    expect(await screen.findByTestId('people-team-config-invalid')).toBeInTheDocument()
    expect(screen.getByTestId('team-config-reason').textContent).toContain('console_capability_missing')
    expect(screen.queryByTestId('shared-access-setup')).toBeNull()
    expect(screen.queryByTestId('remote-control-setup')).toBeNull()
  })

  it('a failed mode read is an error, not a personal Crew', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockRejectedValue(new Error('mode down'))
    mount()
    expect(await screen.findByTestId('people-mode-error')).toBeInTheDocument()
    expect(screen.queryByTestId('shared-access-setup')).toBeNull()
  })

  it('personal: the setup guidance with every adapter shown honestly, plus Remote Control', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockResolvedValue(modeBody('personal'))
    mount()
    expect(await screen.findByTestId('people-personal')).toBeInTheDocument()
    expect(screen.getByTestId('shared-access-setup')).toBeInTheDocument()
    expect(screen.getByTestId('setup-path-kiro_login-status').textContent).toBe('Not available in this build')
    expect(screen.getByTestId('remote-control-setup')).toBeInTheDocument()
  })
})

describe('PeoplePanel direct tailnet link', () => {
  const EMPTY: PeopleResponse = { schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] }
  const DIRECT = {
    state: 'active' as const, reason: '', method: 'localapi_unix', owner_login: 'owner@example.test',
    urls: ['http://crew.tail1234.ts.net:5476/', 'http://100.64.0.1:5476/'], addresses: ['100.64.0.1'],
  }

  it('shows the plain link with copy, the who-can-open note and the device-owner caveat', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', attribution_via: 'tailscale_direct', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    const link = await screen.findByTestId('tailnet-direct-link')
    const field = link.querySelector('input') as HTMLInputElement
    expect(field.value).toBe('http://crew.tail1234.ts.net:5476/')
    expect(field.value).not.toMatch(/token/)
    expect(link).toHaveTextContent('By default, anyone allowed by Tailscale access rules')
    expect(link).toHaveTextContent('With local authorization on')
    expect(screen.getAllByRole('button', { name: 'Copy' }).length).toBeGreaterThan(0)
    expect(screen.getByTestId('people-direct-caveat').textContent).toMatch(/owns each device/)
  })

  it('shows no link when the direct listener is not active', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: { ...DIRECT, state: 'unavailable', reason: 'tailscale_not_running', urls: [] } })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.queryByTestId('tailnet-direct-link')).toBeNull()
    expect(screen.queryByTestId('people-direct-caveat')).toBeNull()
  })

  it('points the macOS empty state at the direct link', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'unavailable', reasons: { attribution: 'platform_unprovable' } })
    people.mockResolvedValue(EMPTY)
    mount()
    expect(await screen.findByTestId('people-unnamed')).toBeInTheDocument()
    expect(screen.getByText(/Share the direct Tailscale link instead to see their names/)).toBeInTheDocument()
  })
})

