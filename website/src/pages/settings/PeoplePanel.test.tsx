import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { ALL_UNAVAILABLE, type PeopleResponse } from '../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { PeoplePanel } from './PeoplePanel'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'

vi.mock('../../api/client', () => ({
  api: { sharedCrewPeople: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), tailscaleInvites: vi.fn(), sharedCrewSetPersonDisplay: vi.fn(), kirocrewConfig: vi.fn(), patchConfig: vi.fn() },
}))
vi.mock('../../components/settingRef/useConfigSchema', () => {
  const schema = new Map([
    ['dashboard.shared_crew.typing_idle_seconds', { path: 'dashboard.shared_crew.typing_idle_seconds', type: 'integer', requiresRestart: true }],
    ['dashboard.shared_crew.tailnet_direct', { path: 'dashboard.shared_crew.tailnet_direct', type: 'boolean', requiresRestart: true }],
  ])
  return {
    useConfigSchema: () => schema,
    useConfigSchemaQuery: () => ({ schema, error: null }),
  }
})
const copy = vi.hoisted(() => vi.fn<(text: string) => Promise<boolean>>())
vi.mock('../../utils/clipboard', () => ({ copyToClipboard: copy }))
const people = vi.mocked(api.sharedCrewPeople)
const caps = vi.mocked(api.sharedCrewCapabilities)
const modeApi = vi.mocked(api.sharedCrewMode)
const configApi = vi.mocked(api.kirocrewConfig)
const patchApi = vi.mocked(api.patchConfig)
const invitesApi = vi.mocked(api.tailscaleInvites)

function modeBody(mode: CrewModeResponse['mode'], over: Partial<CrewModeResponse> = {}): CrewModeResponse {
  return {
    schema_version: 1, mode, crew_id: 'crew-a',
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' },
    adapters: {
      tailscale: { state: 'available', reason: '' },
      kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
    },
    workload_identity_ref: 'unavailable', ...over,
  }
}

const row = (id: string, name: string, over: Partial<PeopleResponse['people'][number]> = {}) => ({
  principal: { id, kind: 'human' as const, display: { name, login: `${id}@example.test`, captured_at: null } },
  sources: ['observed' as const], last_seen_at: 1_700_000_000, devices: 2, sessions_created: 3,
  is_viewer: false, usage: { active: 1, queued: 2 }, ...over,
})

/** Every button except the help glyph next to "tailnet", which opens a definition. */
function actionButtons() {
  return screen.queryAllByRole('button').filter(b => b.getAttribute('aria-label') !== 'More information')
}

/** The text of the ⓘ inside the element with this test id. */
function tipText(testId: string) {
  return screen.getByTestId(testId).querySelector('button[aria-label="More information"]')?.getAttribute('title') ?? ''
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><PeoplePanel /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  // An older gateway: no mode route, so the mode is read from capabilities.
  modeApi.mockResolvedValue(null)
  copy.mockResolvedValue(true)
  configApi.mockResolvedValue({ dashboard: { shared_crew: {} } } as never)
  patchApi.mockResolvedValue({} as never)
  // Not the owner, or no invite routes: the invite card stays hidden.
  invitesApi.mockResolvedValue(null)
})

/** A Crew with the tailnet path set up, so the People list itself renders. */
const SHARED = {
  ...ALL_UNAVAILABLE,
  setup_paths: {
    tailscale: { state: 'available' as const, reason: '' as const },
    kiro_login: { state: 'unavailable' as const, reason: 'not_in_this_build' as const },
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
    expect(screen.getByText('People · 2')).toBeInTheDocument()
    expect(screen.getAllByTestId('person-activity')[0]).toHaveTextContent('p_a@example.test · 1 running')
    expect(screen.queryByText('Assigned users')).toBeNull()
    expect(screen.queryByText('Who can connect')).toBeNull()
    expect(screen.queryByTestId('share-hero-manage')).toBeNull()
    expect(tipText('people-tip')).toMatch(/remove them in Tailscale/)
    // Nothing on this surface manages membership.
    expect(actionButtons()).toHaveLength(0)
    expect(screen.queryByText(/add|remove|role|admin/i, { selector: 'button' })).toBeNull()
  })

  it('shows the console pointer only when the capability names it', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', membership_link: 'console' })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('console-pointer')).toBeInTheDocument()
    expect(tipText('people-tip')).toMatch(/cannot report how much work/)
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
    await screen.findByTestId('people-tip')
    expect(tipText('people-tip')).toMatch(/not running/i)
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
    // Guidance only: no control that does nothing, and links only to existing guides.
    expect(actionButtons()).toHaveLength(0)
    expect(screen.getByTestId('setup-path-tailscale-guide')).toHaveAttribute('href', expect.stringContaining('docs/guides/remote-and-mobile.md#share-one-crew-with-teammates'))
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

  it('network: the connect card and the People list; no adapter rows and no Remote Control', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.getByTestId('share-hero-state')).toHaveTextContent('Available')
    expect(screen.queryByTestId('adapter-tailscale')).toBeNull()
    expect(screen.queryByTestId('adapter-kiro_login')).toBeNull()
    expect(screen.queryByTestId('remote-control-tip')).toBeNull()
    expect(screen.queryByTestId('remote-control-setup')).toBeNull()
    expect(screen.queryByTestId('remote-control-unavailable')).toBeNull()
    expect(screen.queryByTestId('people-console')).toBeNull()
    // Read-only: no invite, revoke or role control.
    expect(actionButtons()).toHaveLength(0)
  })

  it.each(['personal', 'network'] as const)('%s: no Remote Control section even when a phone method exists', async mode => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockResolvedValue(modeBody(mode))
    mount()
    await screen.findByTestId(mode === 'personal' ? 'people-personal' : 'people-panel')
    expect(screen.queryByTestId('remote-control-setup')).toBeNull()
    expect(screen.queryByRole('button', { name: /Remote Control/ })).toBeNull()
    expect(screen.queryByText('Remote Control')).toBeNull()
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
    expect(actionButtons()).toHaveLength(0)
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

  it('personal: the setup guidance with every adapter shown honestly', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockResolvedValue(modeBody('personal'))
    mount()
    expect(await screen.findByTestId('people-personal')).toBeInTheDocument()
    expect(screen.getByTestId('shared-access-setup')).toBeInTheDocument()
    expect(screen.getByTestId('setup-path-kiro_login-status').textContent).toBe('Not available in this build')
  })
})

describe('PeoplePanel layout C', () => {
  const EMPTY: PeopleResponse = { schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] }
  const DIRECT = {
    state: 'active' as const, reason: '', method: 'localapi_unix', owner_login: 'owner@example.test',
    urls: ['http://100.64.0.1:5476/', 'http://crew.tail1234.ts.net:5476/'], addresses: ['100.64.0.1'],
  }
  const INVITES = {
    connected: true, tailnet: 'example', key_expires: '', key_expires_soon: false, key_expired: false,
    admin_share_url: '', invites: [{ id: 'inv1', kind: 'email' as const, email: 'pat@example.test', multi_use: false, alias: 'Pat', avatar_id: '', created_at: 0, status: 'pending' as const, accepted_login: '' }],
  }

  it('shows the live state, the first link with Copy link for a teammate, who is here now and what they are doing', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', attribution_via: 'tailscale_direct', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [
      row('p_b', 'Bob', { live: { activity: 'typing', session_key: 's1', session_title: 'Release plan' } }),
      row('p_c', 'Cleo', { last_seen_at: null }),
    ] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    const hero = await screen.findByTestId('share-hero')
    expect(screen.getByTestId('share-hero-state')).toHaveTextContent('Shared on your tailnet')
    expect(screen.queryByTestId('share-hero-tip')).toBeNull()
    expect(screen.getByTestId('share-hero-link')).toHaveValue('http://100.64.0.1:5476/')
    expect(screen.getByTestId('share-hero-copy')).toHaveTextContent('Copy link')
    expect(screen.queryByTestId('share-hero-step-share')).toBeNull()
    expect(screen.getByTestId('share-hero-here')).toHaveTextContent('1 here now')
    expect(hero.textContent).not.toMatch(/reachable/i)
    const bob = screen.getByTestId('person-p_b')
    expect(bob).toHaveTextContent('typing in Release plan')
    expect(bob.querySelector('[aria-label="Here now"]')).not.toBeNull()
    expect(screen.getByTestId('person-p_c').querySelector('[aria-label="Here now"]')).toBeNull()
  })

  it('counts the viewer as here now and lets them edit their own row', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [
      row('p_me', 'Me', { is_viewer: true, last_seen_at: 1_700_000_000, tailscale_user_id: '11' }),
      row('p_c', 'Cleo'),
    ] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    const me = await screen.findByTestId('person-p_me')
    expect(me).toHaveTextContent('Here now')
    expect(me).not.toHaveTextContent('Last seen')
    expect(me.querySelector('[aria-label="Here now"]')).not.toBeNull()
    expect(me.querySelector('[data-testid="person-edit"]')).not.toBeNull()
    expect(screen.getByTestId('share-hero-here')).toHaveTextContent('1 here now')
    expect(tipText('people-tip')).toMatch(/^To remove someone, remove them in Tailscale\./)
  })

  it('shows the setup state and no link while the listener is off', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: { ...DIRECT, state: 'off', urls: [] } })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('share-hero-state')).toHaveTextContent('Available')
    expect(screen.queryByText('Shared on your tailnet')).toBeNull()
    expect(screen.queryByTestId('share-hero-link')).toBeNull()
    expect(screen.queryByTestId('share-hero-here')).toBeNull()
  })

  it('offers Invite... to the owner, opens the dialog, and lists a pending invite dimmed with Revoke', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    invitesApi.mockResolvedValue(INVITES as never)
    mount()
    const pending = await screen.findByTestId('invite-inv1')
    expect(pending.className).toMatch(/opacity-60/)
    expect(pending).toHaveTextContent('Revoke')
    expect(pending).not.toHaveTextContent('Resend')
    fireEvent.click(screen.getByTestId('share-hero-invite'))
    expect(await screen.findByTestId('invite-dialog')).toBeInTheDocument()
    expect(screen.getByTestId('invite-form')).toBeInTheDocument()
  })

  it('hides Invite... and reads no invites when policy pins tailnet access off', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT, invites: { state: 'unavailable', reason: 'governance_pinned_off' } })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    invitesApi.mockResolvedValue(INVITES as never)
    mount()
    await screen.findByTestId('person-p_b')
    expect(screen.queryByTestId('share-hero-invite')).toBeNull()
    expect(screen.queryByTestId('people-invites-error')).toBeNull()
    expect(invitesApi).not.toHaveBeenCalled()
  })

  it('hides Invite... and the read invites once a policy pin lands after the read', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    invitesApi.mockResolvedValue(INVITES as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><PeoplePanel /></QueryClientProvider>)
    expect(await screen.findByTestId('share-hero-invite')).toBeInTheDocument()
    expect(screen.getByTestId('invite-inv1')).toBeInTheDocument()
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT, invites: { state: 'unavailable', reason: 'governance_pinned_off' } })
    await act(async () => { await qc.refetchQueries({ queryKey: sharedCrewKeys.capabilities }) })
    await waitFor(() => expect(screen.queryByTestId('share-hero-invite')).toBeNull())
    expect(screen.queryByTestId('invite-inv1')).toBeNull()
  })

  it('asks for the API key first inside the dialog when none is connected', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    invitesApi.mockResolvedValue({ ...INVITES, connected: false, invites: [] } as never)
    mount()
    fireEvent.click(await screen.findByTestId('share-hero-invite'))
    const dialog = await screen.findByTestId('invite-dialog')
    expect(dialog).toHaveTextContent('Connect a Tailscale API key first')
    expect(screen.getByTestId('invite-key-input')).toBeInTheDocument()
    expect(screen.queryByTestId('invite-form')).toBeNull()
  })

  it('hides Invite... from a teammate (the invite read refuses)', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    await screen.findByTestId('share-hero')
    expect(screen.queryByTestId('share-hero-invite')).toBeNull()
  })

  it('lets anyone edit a person with a known Tailscale user id, and saves name and picture', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [
      row('p_b', 'Bob', { tailscale_user_id: '4102', member: { alias: 'Bobby', avatar_id: '', profile_pic_url: '' } }),
      row('p_c', 'Cleo'),
    ] })
    modeApi.mockResolvedValue(modeBody('network'))
    vi.mocked(api.sharedCrewSetPersonDisplay).mockResolvedValue({ user_id: '4102', alias: 'Robert', avatar_id: '', audited: true })
    mount()
    const bob = await screen.findByTestId('person-p_b')
    expect(bob).toHaveTextContent('Bobby')
    expect(screen.getByTestId('person-p_c').querySelector('[data-testid="person-edit"]')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Edit Bobby' }))
    const dialog = await screen.findByTestId('person-edit-dialog')
    expect(dialog).toHaveTextContent('p_b@example.test')
    expect(dialog).toHaveTextContent('It does not change who can connect.')
    fireEvent.change(screen.getByTestId('person-edit-name'), { target: { value: 'Robert' } })
    fireEvent.click(screen.getByTestId('person-edit-save'))
    await waitFor(() => expect(api.sharedCrewSetPersonDisplay).toHaveBeenCalledWith('4102', { alias: 'Robert' }))
    await waitFor(() => expect(screen.queryByTestId('person-edit-dialog')).toBeNull())
  })

  it('waits for a chosen picture to finish reading before Save can send it', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob', { tailscale_user_id: '4102' })] })
    modeApi.mockResolvedValue(modeBody('network'))
    vi.mocked(api.sharedCrewSetPersonDisplay).mockResolvedValue({ user_id: '4102', alias: '', avatar_id: 'a1', audited: true })
    mount()
    fireEvent.click(await screen.findByTestId('person-edit'))
    let finish: (b: ArrayBuffer) => void = () => {}
    const file = new File(['png'], 'me.png', { type: 'image/png' })
    Object.defineProperty(file, 'arrayBuffer', { value: () => new Promise<ArrayBuffer>(r => { finish = r }) })
    fireEvent.change(await screen.findByTestId('person-edit-picture-input'), { target: { files: [file] } })
    await waitFor(() => expect(screen.getByTestId('person-edit-save')).toBeDisabled())
    fireEvent.submit(screen.getByTestId('person-edit-dialog'))
    expect(api.sharedCrewSetPersonDisplay).not.toHaveBeenCalled()
    finish(new TextEncoder().encode('png').buffer as ArrayBuffer)
    await waitFor(() => expect(screen.getByTestId('person-edit-save')).not.toBeDisabled())
    // The preview is a data: URL of the read bytes under an allow-listed type.
    expect(screen.getByTestId('person-edit-preview')).toHaveAttribute('src', `data:image/png;base64,${btoa('png')}`)
    fireEvent.click(screen.getByTestId('person-edit-save'))
    await waitFor(() => expect(api.sharedCrewSetPersonDisplay).toHaveBeenCalledWith('4102', expect.objectContaining({ avatar_data_base64: expect.any(String) })))
  })

  it('shows a failed picture read as an error notice and keeps the name', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob', { tailscale_user_id: '4102' })] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    fireEvent.click(await screen.findByTestId('person-edit'))
    fireEvent.change(screen.getByTestId('person-edit-name'), { target: { value: 'Robert' } })
    const file = new File(['png'], 'me.png', { type: 'image/png' })
    Object.defineProperty(file, 'arrayBuffer', { value: () => Promise.reject(new Error('unreadable')) })
    fireEvent.change(screen.getByTestId('person-edit-picture-input'), { target: { files: [file] } })
    const notice = await screen.findByTestId('person-edit-read-error')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(screen.queryByTestId('person-edit-hint')).toBeNull()
    expect(screen.getByTestId('person-edit-name')).toHaveValue('Robert')
    expect(api.sharedCrewSetPersonDisplay).not.toHaveBeenCalled()
  })

  it('refuses a picture of the wrong type before uploading', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob', { tailscale_user_id: '4102' })] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    fireEvent.click(await screen.findByTestId('person-edit'))
    fireEvent.change(await screen.findByTestId('person-edit-picture-input'), { target: { files: [new File(['x'], 'x.svg', { type: 'image/svg+xml' })] } })
    expect(await screen.findByTestId('person-edit-hint')).toBeInTheDocument()
    expect(api.sharedCrewSetPersonDisplay).not.toHaveBeenCalled()
  })

  it('points the macOS empty state at the direct link', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'unavailable', reasons: { attribution: 'platform_unprovable' } })
    people.mockResolvedValue(EMPTY)
    mount()
    expect(await screen.findByTestId('people-unnamed')).toBeInTheDocument()
    expect(screen.getByText(/Share the direct Tailscale link instead to see their names/)).toBeInTheDocument()
  })
})

describe('PeoplePanel connect card, owner rows and Advanced', () => {
  const EMPTY: PeopleResponse = { schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] }
  const DIRECT = {
    state: 'active' as const, reason: '', method: 'localapi_unix', owner_login: 'owner@example.test',
    urls: ['http://100.64.0.1:5476/', 'http://crew.tail1234.ts.net:5476/', 'http://[fd7a::1]:5476/'], addresses: ['100.64.0.1'],
  }
  const connected = (invites: unknown[] = []) => ({
    connected: true, tailnet: 'example', key_expires: '', key_expires_soon: false, key_expired: false,
    admin_share_url: '', invites,
  })
  const ownerRows = () => [
    row('p_me', 'Mac Studio', { is_viewer: true, is_owner: true, last_seen_at: 1_700_000_000 }),
    row('p_s', 'Sam', { sources: ['tailscale_share' as const], last_seen_at: null, devices: 0 }),
    row('p_c', 'Cleo', { last_seen_at: null, devices: 0 }),
  ]

  // Capabilities, then People and mode, then the invite read: waits that span
  // that chain get this ceiling.
  const OWNER_CHAIN_TIMEOUT_MS = 5000

  async function mountOwner(over: Partial<PeopleResponse> = {}) {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: ownerRows(), ...over })
    modeApi.mockResolvedValue(modeBody('network'))
    invitesApi.mockResolvedValue(connected() as never)
    mount()
    await screen.findByTestId('share-hero-invite', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })
    return screen.getByTestId('share-hero')
  }

  it('gives the owner two steps: share on Tailscale, then send the Crew link', async () => {
    await mountOwner()
    const share = screen.getByTestId('share-hero-step-share')
    const send = screen.getByTestId('share-hero-step-send')
    expect(share).toHaveTextContent('Share this machine on Tailscale')
    expect(share).toContainElement(screen.getByTestId('share-hero-invite'))
    expect(share).toContainElement(screen.getByTestId('share-hero-manage'))
    expect(send).toHaveTextContent('Send them the Crew link')
    expect(screen.getByTestId('share-hero-link')).toHaveValue('http://100.64.0.1:5476/')
    expect(screen.getByTestId('share-hero-copy-message')).toHaveTextContent('Copy invite message')
    expect(screen.queryByTestId('share-hero-copy')).toBeNull()
    // Two steps, nothing explaining them.
    expect(screen.getByTestId('share-hero').querySelectorAll('ol > li')).toHaveLength(2)
    expect(screen.getByTestId('share-hero').querySelector('details')?.textContent).toContain('crew.tail1234.ts.net')
  })

  it('copies the invite message: owner name, IPv4 link, then the MagicDNS fallback, with the email line', async () => {
    await mountOwner()
    fireEvent.click(screen.getByTestId('share-hero-copy-message'))
    await waitFor(() => expect(copy).toHaveBeenCalledTimes(1))
    const text = copy.mock.calls[0][0]
    expect(text.split('\n')).toEqual([
      'Mac Studio invited you to Kiro Crew.',
      '1. Accept the invitation Tailscale emailed you',
      '2. Open Crew: http://100.64.0.1:5476/',
      '',
      "Can't open it? Turn on MagicDNS at https://login.tailscale.com/admin/dns, then open http://crew.tail1234.ts.net:5476/",
    ])
    expect(await screen.findByTestId('share-hero-copy-message')).toHaveTextContent('Copied')
  })

  it('does not use a pending link invite in the connect card message', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: ownerRows() })
    modeApi.mockResolvedValue(modeBody('network'))
    const inviteUrl = 'https://login.tailscale.com/uinv/abc'
    invitesApi.mockResolvedValue(connected([
      { id: 'l1', kind: 'link', email: '', multi_use: false, alias: '', avatar_id: '', created_at: 0, status: 'pending', accepted_login: '', invite_url: inviteUrl },
    ]) as never)
    mount()
    await screen.findByTestId('invite-l1')
    fireEvent.click(screen.getByTestId('share-hero-copy-message'))
    await waitFor(() => expect(copy).toHaveBeenCalledTimes(1))
    const text = copy.mock.calls[0][0]
    expect(text).not.toContain(inviteUrl)
    expect(text).toContain('1. Accept the invitation Tailscale emailed you')
  })

  it('shows a failed copy as an error notice', async () => {
    copy.mockResolvedValue(false)
    await mountOwner()
    fireEvent.click(screen.getByTestId('share-hero-copy-message'))
    expect(await screen.findByTestId('share-hero-copy-message-error')).toHaveAttribute('role', 'alert')
  })

  it('labels the owner row "You · Owner" and a never-seen sharee "Not joined"; others never seen carry no narration', async () => {
    await mountOwner()
    const me = screen.getByTestId('person-p_me')
    expect(me).toHaveTextContent('You · Owner')
    expect(me).toHaveTextContent('Here now')
    expect(screen.getByTestId('person-p_s')).toHaveTextContent('Not joined')
    const cleo = screen.getByTestId('person-p_c')
    expect(cleo).not.toHaveTextContent('Not joined')
    expect(cleo).not.toHaveTextContent('Not seen')
    expect(cleo.querySelector('[data-testid="person-activity"]')?.textContent).toBe('p_c@example.test')
  })

  it('shows a teammate the owner badge on the owner row, not "You"', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_o', 'Mac Studio', { is_owner: true }), row('p_me', 'Pat', { is_viewer: true })] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    const owner = await screen.findByTestId('person-p_o')
    expect(owner).toHaveTextContent('Crew owner')
    expect(owner).not.toHaveTextContent('You')
    expect(screen.queryByTestId('people-advanced')).toBeNull()
  })

  it('offers Advanced to the owner only, collapsed, and writes each field to its dashboard.shared_crew path', async () => {
    await mountOwner()
    const advanced = await screen.findByTestId('people-advanced') as HTMLDetailsElement
    expect(advanced.open).toBe(false)
    expect(advanced.querySelector('summary')).toHaveTextContent('Advanced')
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Fair admission' })).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(screen.getByRole('switch', { name: 'Fair admission' }))
    await waitFor(() => expect(patchApi).toHaveBeenCalledWith('dashboard.shared_crew.admission_enabled', true))
    fireEvent.click(screen.getByRole('switch', { name: 'Direct tailnet link' }))
    await waitFor(() => expect(patchApi).toHaveBeenCalledWith('dashboard.shared_crew.tailnet_direct', true))
    expect(advanced).toHaveTextContent('changes need a restart')
    const writeNumber = (testId: string, value: string) => {
      const input = screen.getByTestId(testId)
      fireEvent.change(input, { target: { value } })
      fireEvent.blur(input)
    }
    writeNumber('people-advanced-principal_concurrency', '6')
    await waitFor(() => expect(patchApi).toHaveBeenCalledWith('dashboard.shared_crew.principal_concurrency', 6))
    writeNumber('people-advanced-crew_concurrency', '12')
    await waitFor(() => expect(patchApi).toHaveBeenCalledWith('dashboard.shared_crew.crew_concurrency', 12))
    writeNumber('people-advanced-typing_idle_seconds', '20')
    await waitFor(() => expect(patchApi).toHaveBeenCalledWith('dashboard.shared_crew.typing_idle_seconds', 20))
  })

  it('stacks Advanced labels before wrapping controls on narrow panes', async () => {
    await mountOwner()
    const input = await screen.findByTestId('people-advanced-typing_idle_seconds')
    const controls = input.parentElement as HTMLElement
    const layout = controls.parentElement as HTMLElement
    expect(layout).toHaveClass('flex-col', 'sm:flex-row', 'sm:items-center', 'sm:justify-between')
    expect(layout.className.split(/\s+/)).not.toContain('flex-row')
    expect(controls).toHaveClass('flex-wrap')
  })

  it('marks the typing window as applying after a restart, and the live limits not', async () => {
    await mountOwner()
    const controls = (control: HTMLElement) => control.parentElement as HTMLElement
    await waitFor(() => expect(screen.getAllByTestId('restart-required-badge')).toHaveLength(2))
    expect(controls(screen.getByTestId('people-advanced-typing_idle_seconds')).querySelector('[data-testid="restart-required-badge"]')).not.toBeNull()
    expect(controls(screen.getByRole('switch', { name: 'Direct tailnet link' })).querySelector('[data-testid="restart-required-badge"]')).not.toBeNull()
    expect(controls(screen.getByTestId('people-advanced-principal_concurrency')).querySelector('[data-testid="restart-required-badge"]')).toBeNull()
    expect(controls(screen.getByTestId('people-advanced-crew_concurrency')).querySelector('[data-testid="restart-required-badge"]')).toBeNull()
    expect(controls(screen.getByRole('switch', { name: 'Fair admission' })).querySelector('[data-testid="restart-required-badge"]')).toBeNull()
  })

  it('writes nothing for a value outside the gateway bounds', async () => {
    await mountOwner()
    await waitFor(() => expect(screen.getByTestId('people-advanced-principal_concurrency')).not.toBeDisabled())
    const input = screen.getByTestId('people-advanced-principal_concurrency')
    fireEvent.change(input, { target: { value: '0' } })
    expect(input).toHaveAttribute('aria-invalid', 'true')
    fireEvent.blur(input)
    expect(patchApi).not.toHaveBeenCalled()
    expect(input).toHaveValue(4)
  })

  it('reads stored values and shows a failed save as an error notice', async () => {
    configApi.mockResolvedValue({ dashboard: { shared_crew: { admission_enabled: true, principal_concurrency: 2, crew_concurrency: 8, typing_idle_seconds: 30 } } } as never)
    patchApi.mockRejectedValue(new Error('nope'))
    await mountOwner()
    await waitFor(() => expect(screen.getByTestId('people-advanced-principal_concurrency')).toHaveValue(2))
    expect(screen.getByRole('switch', { name: 'Fair admission' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('people-advanced-crew_concurrency')).toHaveValue(8)
    expect(screen.getByTestId('people-advanced-typing_idle_seconds')).toHaveValue(30)
    fireEvent.click(screen.getByRole('switch', { name: 'Fair admission' }))
    expect(await screen.findByTestId('people-advanced-save-error')).toHaveAttribute('role', 'alert')
  })

  it('locks every Advanced control through a save and its config read-back', async () => {
    const stale = { dashboard: { shared_crew: { admission_enabled: false } } }
    const accepted = { dashboard: { shared_crew: { admission_enabled: true } } }
    let finishPatch: (value: unknown) => void = () => {}
    let finishRefetch: (value: unknown) => void = () => {}
    patchApi.mockImplementationOnce(() => new Promise(resolve => { finishPatch = resolve }) as ReturnType<typeof api.patchConfig>)
    configApi
      .mockResolvedValueOnce(stale as never)
      .mockImplementationOnce(() => new Promise(resolve => { finishRefetch = resolve }) as ReturnType<typeof api.kirocrewConfig>)
    await mountOwner()
    const admission = () => screen.getByRole('switch', { name: 'Fair admission' })
    const expectLocked = (locked: boolean) => {
      for (const toggle of [admission(), screen.getByRole('switch', { name: 'Direct tailnet link' })]) {
        if (locked) expect(toggle).toHaveAttribute('aria-disabled', 'true')
        else expect(toggle).not.toHaveAttribute('aria-disabled')
      }
      for (const input of [
        screen.getByTestId('people-advanced-principal_concurrency'),
        screen.getByTestId('people-advanced-crew_concurrency'),
        screen.getByTestId('people-advanced-typing_idle_seconds'),
      ]) {
        if (locked) expect(input).toBeDisabled()
        else expect(input).not.toBeDisabled()
      }
    }
    await waitFor(() => expectLocked(false))

    fireEvent.click(admission())
    await waitFor(() => expect(patchApi).toHaveBeenCalledWith('dashboard.shared_crew.admission_enabled', true))
    expect(admission()).toHaveAttribute('aria-checked', 'true')
    expectLocked(true)

    await act(async () => { finishPatch({}) })
    await waitFor(() => expect(configApi).toHaveBeenCalledTimes(2))
    expect(admission()).toHaveAttribute('aria-checked', 'true')
    expectLocked(true)

    await act(async () => { finishRefetch(accepted) })
    await waitFor(() => expectLocked(false))
    expect(admission()).toHaveAttribute('aria-checked', 'true')
  })

  it('shows a refused Advanced save and restores the server value', async () => {
    configApi.mockResolvedValue({ dashboard: { shared_crew: { admission_enabled: false } } } as never)
    patchApi.mockRejectedValueOnce(new ApiError(409, 'config changed'))
    await mountOwner()
    const admission = () => screen.getByRole('switch', { name: 'Fair admission' })
    await waitFor(() => expect(admission()).toHaveAttribute('aria-checked', 'false'))

    fireEvent.click(admission())

    expect(await screen.findByTestId('people-advanced-save-error')).toHaveAttribute('role', 'alert')
    expect(admission()).toHaveAttribute('aria-checked', 'false')
  })

  it('personal mode: the owner gets Advanced after the invite card', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockResolvedValue(modeBody('personal'))
    invitesApi.mockResolvedValue(connected() as never)
    mount()
    expect(await screen.findByTestId('people-advanced')).toBeInTheDocument()
  })
})
