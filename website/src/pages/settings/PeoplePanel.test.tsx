import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ALL_UNAVAILABLE, type PeopleResponse } from '../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { PeoplePanel } from './PeoplePanel'

vi.mock('../../api/client', () => ({
  api: { sharedCrewPeople: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), mobileConnectMethods: vi.fn(), tailscaleInvites: vi.fn(), sharedCrewAccess: vi.fn(), sharedCrewSetPersonDisplay: vi.fn() },
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
    expect(screen.getByText('People · 2')).toBeInTheDocument()
    expect(screen.getAllByTestId('person-activity')[0]).toHaveTextContent('p_a@example.test · 1 running')
    expect(screen.queryByText('Assigned users')).toBeNull()
    expect(screen.queryByText('Who can connect')).toBeNull()
    expect(screen.getByTestId('share-hero-manage')).toHaveAttribute('href', 'https://login.tailscale.com/admin/users')
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
    expect(screen.getByTestId('setup-path-systems_manager-status').textContent).toBe('Not available in this build')
    // Guidance only: no control that does nothing, and links only to existing guides.
    expect(actionButtons()).toHaveLength(0)
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

  it('network: the share card, the People list and Remote Control; no adapter rows', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.getByTestId('share-hero-state')).toHaveTextContent('Available')
    expect(screen.queryByTestId('adapter-tailscale')).toBeNull()
    expect(screen.queryByTestId('adapter-systems_manager')).toBeNull()
    expect(screen.queryByTestId('adapter-kiro_login')).toBeNull()
    expect(tipText('remote-control-tip')).toMatch(/for you only/)
    expect(screen.getByTestId('remote-control-setup')).toBeInTheDocument()
    expect(await screen.findByTestId('remote-control-unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('people-console')).toBeNull()
    // Read-only: no invite, revoke or role control.
    expect(actionButtons()).toHaveLength(0)
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

describe('PeoplePanel layout C', () => {
  const EMPTY: PeopleResponse = { schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] }
  const DIRECT = {
    state: 'active' as const, reason: '', method: 'localapi_unix', owner_login: 'owner@example.test',
    urls: ['http://crew.tail1234.ts.net:5476/', 'http://100.64.0.1:5476/'], addresses: ['100.64.0.1'],
  }
  const INVITES = {
    connected: true, tailnet: 'example', key_expires: '', key_expires_soon: false, key_expired: false,
    admin_share_url: '', invites: [{ id: 'inv1', kind: 'email' as const, email: 'pat@example.test', multi_use: false, alias: 'Pat', avatar_id: '', created_at: 0, status: 'pending' as const, accepted_login: '' }],
  }

  it('shows the live state, the link with Copy link, who is here now and what they are doing', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', attribution_via: 'tailscale_direct', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [
      row('p_b', 'Bob', { live: { activity: 'typing', session_key: 's1', session_title: 'Release plan' } }),
      row('p_c', 'Cleo', { last_seen_at: null }),
    ] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    const hero = await screen.findByTestId('share-hero')
    expect(screen.getByTestId('share-hero-state')).toHaveTextContent('Shared on your tailnet')
    expect(tipText('share-hero-tip')).toMatch(/Messages still go to the AI model service/)
    expect(screen.getByTestId('share-hero-link')).toHaveValue('http://crew.tail1234.ts.net:5476/')
    expect(screen.getByTestId('share-hero-copy')).toHaveTextContent('Copy link')
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
    expect(tipText('people-tip')).toMatch(/Tailscale names the person who owns each device/)
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
