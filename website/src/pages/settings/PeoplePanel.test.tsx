import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { i18nT } from '../../i18n/t'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { ALL_UNAVAILABLE, type PeopleResponse } from '../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../api/sharedCrewMode'
import { PeoplePanel } from './PeoplePanel'
import { sharedCrewKeys } from '../../hooks/sharedCrew/sharedCrewSocket'
import { tailscaleInvitesKey } from './TailscaleInvites'

vi.mock('../../api/client', () => ({
  api: { sharedCrewPeople: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), tailscaleInvites: vi.fn(), sharedCrewSsm: vi.fn(), tailscaleRevokeInvite: vi.fn(), sharedCrewSetPersonDisplay: vi.fn(), kirocrewConfig: vi.fn(), patchConfig: vi.fn(), viewerOwnerOk: vi.fn() },
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

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><PeoplePanel /></QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
  // The owner by default; the teammate case overrides it for itself only.
  vi.mocked(api.viewerOwnerOk).mockReset().mockResolvedValue(true)
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

/** Capabilities, then People and mode, then the invite read: waits that span
 *  that chain get this ceiling. */
const OWNER_CHAIN_TIMEOUT_MS = 5000

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
    expect(screen.queryByTestId('people-tip')).toBeNull()
    // Nothing on this surface manages membership.
    expect(actionButtons()).toHaveLength(0)
    expect(screen.queryByText(/add|remove|role|admin/i, { selector: 'button' })).toBeNull()
  })

  it('shows the console pointer only when the capability names it', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', membership_link: 'console' })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    expect(await screen.findByTestId('console-pointer')).toBeInTheDocument()
    expect(screen.queryByTestId('people-tip')).toBeNull()
  })

  it('shows a capabilities read failure with agent recovery, never unavailable attribution', async () => {
    caps.mockRejectedValue(new Error('capabilities read failed'))
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('people-capabilities-error')).toHaveTextContent('capabilities read failed')
    expect(screen.getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
    expect(screen.queryByTestId('people-unavailable')).toBeNull()
    expect(screen.queryByTestId('people-personal')).toBeNull()
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

  it('says in one inline line, with no tip icon, when Tailscale was not running at start', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'degraded', reasons: { attribution: 'daemon_not_found' } })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: true, people: [] })
    mount()
    const note = await screen.findByTestId('people-note')
    expect(note).toHaveTextContent(/not running/i)
    expect(screen.getAllByTestId('people-note')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'More information' })).toBeNull()
  })

  it('says inline when this Crew cannot report work per person, and not when it can', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [row('p_a', 'Ann')] })
    const first = mount()
    expect(await screen.findByTestId('people-note')).toHaveTextContent(/cannot report how much work/i)
    first.unmount()
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: true, people: [row('p_a', 'Ann')] })
    mount()
    expect(await screen.findByTestId('people-list')).toBeInTheDocument()
    expect(screen.queryByTestId('people-note')).toBeNull()
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
    expect(screen.queryByTestId('people-personal')).toBeNull()
    expect(screen.queryByText(/only you can use it/)).toBeNull()
  })

  it('keeps the sources not set up when only the viewer has been seen', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue({
      schema_version: 1, directory: { state: 'unavailable' }, usage_available: false,
      people: [row('p_a', 'Alice', { is_viewer: true })],
    })
    mount()
    expect(await screen.findByTestId('people-personal')).toBeInTheDocument()
    expect(screen.getByTestId('source-tailscale')).toHaveAttribute('data-state', 'off')
  })

  it('shows each source not set up as a dashed card with its setup guide when no shared-access path is configured', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] })
    mount()
    const tailscale = await screen.findByTestId('source-tailscale')
    expect(screen.queryByTestId('people-panel')).toBeNull()
    expect(tailscale).toHaveAttribute('data-state', 'off')
    expect(tailscale).toHaveClass('border-dashed')
    expect(screen.getByTestId('source-tailscale-status').textContent).toBe('Not set up')
    expect(screen.getByTestId('source-ssm')).toHaveClass('border-dashed')
    // Your organization and Kiro Login are not set up here, so they show only while on.
    expect(screen.queryByTestId('source-organization')).toBeNull()
    expect(screen.queryByTestId('source-kiro_login')).toBeNull()
    // Guidance only: the setup buttons open existing guides.
    expect(actionButtons()).toHaveLength(0)
    expect(screen.getByTestId('source-tailscale-setup')).toHaveTextContent('Set up Tailscale')
    expect(screen.getByTestId('source-tailscale-setup')).toHaveAttribute('href', expect.stringContaining('docs/guides/remote-and-mobile.md#share-one-crew-with-teammates'))
    expect(screen.getByTestId('source-ssm-setup')).toHaveTextContent('Set up AWS SSM')
  })

  it('shows the sources on an older gateway without the routes', async () => {
    caps.mockResolvedValue(null)
    people.mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('source-tailscale')).toHaveAttribute('data-state', 'off')
  })

  it('names each source with the availability the capabilities report', async () => {
    caps.mockResolvedValue({
      ...ALL_UNAVAILABLE,
      setup_paths: {
        tailscale: { state: 'degraded', reason: 'tailnet_unreachable' },
        kiro_login: { state: 'unavailable', reason: 'not_in_this_build' },
      },
    })
    people.mockResolvedValue(null)
    mount()
    expect(await screen.findByTestId('source-tailscale')).toHaveAttribute('data-state', 'on')
    expect(screen.getByTestId('source-tailscale-status').textContent).toBe('On, but not reachable')
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
    expect(screen.queryByTestId('people-personal')).toBeNull()
  })
})

describe('PeoplePanel modes', () => {
  const EMPTY: PeopleResponse = { schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] }

  it('network: the source cards and the People list; no adapter rows and no Remote Control', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('people-panel')).toBeInTheDocument()
    expect(screen.getByTestId('source-tailscale-status')).toHaveTextContent('Connected')
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

  it('console: the read-only organization card with the validated Console link, and its people "Managed by admin"', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('console', {
      team_config: { state: 'valid', reason: '', capability: '', cause: '' },
      console: { state: 'available', url: 'https://console.example.test/crews/1' },
      adapters: { tailscale: { state: 'unavailable', reason: 'not_configured' } },
    }))
    mount()
    const org = await screen.findByTestId('source-organization')
    expect(org).toHaveAttribute('data-state', 'on')
    expect(within(org).getByTestId('source-organization-status')).toHaveTextContent('Managed by admin')
    expect(within(org).getByTestId('source-organization-people')).toHaveTextContent('People: 1')
    expect(within(org).getByTestId('console-link')).toHaveAttribute('href', 'https://console.example.test/crews/1')
    expect(within(org).getByTestId('console-link')).toHaveTextContent('Open console')
    const bob = screen.getByTestId('person-p_b')
    expect(within(bob).getByTestId('person-source')).toHaveTextContent('Your organization')
    expect(within(bob).getByTestId('person-managed')).toHaveTextContent('Managed by admin')
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
    expect(screen.queryByTestId('people-personal')).toBeNull()
    expect(screen.queryByTestId('remote-control-setup')).toBeNull()
  })

  it('a failed mode read is an error, not a personal Crew', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockRejectedValue(new Error('mode down'))
    mount()
    expect(await screen.findByTestId('people-mode-error')).toBeInTheDocument()
    expect(screen.queryByTestId('people-personal')).toBeNull()
  })

  it('personal: each source shown honestly, the ones not set up dashed', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    modeApi.mockResolvedValue(modeBody('personal', { adapters: { tailscale: { state: 'unavailable', reason: 'not_configured' } } }))
    mount()
    expect(await screen.findByTestId('people-personal')).toBeInTheDocument()
    expect(screen.getByTestId('source-tailscale')).toHaveAttribute('data-state', 'off')
    expect(screen.getByTestId('source-ssm')).toHaveAttribute('data-state', 'off')
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
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(false)
    mount()
    await screen.findByTestId('source-tailscale')
    expect(screen.getByTestId('source-tailscale-body')).toHaveTextContent('Reachable at 100.64.0.1')
    expect(await screen.findByTestId('share-hero-link')).toHaveValue('http://100.64.0.1:5476/')
    expect(await screen.findByTestId('share-hero-copy')).toHaveTextContent('Copy link')
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
  })

  it('shows the Tailscale status and no link while the listener is off', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: { ...DIRECT, state: 'off', urls: [] } })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    expect(await screen.findByTestId('source-tailscale-status')).toHaveTextContent('Connected')
    expect(screen.getByTestId('source-tailscale-body')).not.toHaveTextContent('Reachable at')
    expect(screen.queryByTestId('share-hero-link')).toBeNull()
  })

  it('offers Invite... to the owner, opens the dialog, and lists a pending invite in the People list with a Sent tag, its chip and Remove', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    invitesApi.mockResolvedValue(INVITES as never)
    mount()
    const pending = await screen.findByTestId('invite-inv1', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })
    expect(within(pending).getByText('Sent')).toBeInTheDocument()
    expect(within(pending).getByTestId('person-source')).toHaveTextContent('Tailscale')
    // A row of the one list, below the people it is not counted in.
    expect(screen.getByTestId('people-list')).toContainElement(pending)
    expect(pending).not.toHaveTextContent('Waiting')
    expect(pending).toHaveTextContent('Remove')
    expect(pending).not.toHaveTextContent('Revoke')
    // N counts people; the invite is not one yet.
    expect(screen.getByText('People · 1')).toBeInTheDocument()
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
    expect(await screen.findByTestId('share-hero-invite', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })).toBeInTheDocument()
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
    fireEvent.click(await screen.findByTestId('share-hero-invite', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS }))
    const dialog = await screen.findByTestId('invite-dialog')
    expect(dialog).toHaveTextContent('Connect a Tailscale API key first.')
    expect(screen.getByTestId('invite-key-input')).toBeInTheDocument()
    expect(screen.queryByTestId('invite-form')).toBeNull()
  })

  it('hides Invite... from a teammate (the invite read refuses)', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_b', 'Bob')] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    await screen.findByTestId('source-tailscale')
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

  it('ignores an Enter that lands after the picture read ends but before the dialog shows it', async () => {
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
    // act holds the read's render until it returns, and the zero-delay task
    // lets the read's promise chain finish first: the dialog on screen still
    // shows the read in progress when the Enter arrives.
    await act(async () => {
      finish(new TextEncoder().encode('png').buffer as ArrayBuffer)
      await new Promise(r => setTimeout(r, 0))
      fireEvent.submit(screen.getByTestId('person-edit-dialog'))
    })
    expect(api.sharedCrewSetPersonDisplay).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('person-edit-save'))
    await waitFor(() => expect(api.sharedCrewSetPersonDisplay).toHaveBeenCalledWith('4102', expect.objectContaining({ avatar_data_base64: btoa('png') })))
    expect(api.sharedCrewSetPersonDisplay).toHaveBeenCalledTimes(1)
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
    const hint = await screen.findByTestId('person-edit-hint')
    expect(hint).toHaveAttribute('role', 'status')
    expect(screen.getByRole('button', { name: i18nT('sharedCrew.edit_picture') })).toHaveAttribute('aria-describedby', hint.id)
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


  async function mountOwner(over: Partial<PeopleResponse> = {}) {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: ownerRows(), ...over })
    modeApi.mockResolvedValue(modeBody('network'))
    invitesApi.mockResolvedValue(connected() as never)
    mount()
    await screen.findByTestId('share-hero-invite', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })
    return screen.getByTestId('source-tailscale')
  }

  it('gives the owner one Invite… on the Tailscale card beside the Tailscale link, with labels only', async () => {
    const hero = await mountOwner()
    const entry = screen.getByTestId('share-hero-invite')
    expect(entry).toHaveTextContent('Invite…')
    expect(screen.getAllByTestId('share-hero-invite')).toHaveLength(1)
    expect(screen.getByTestId('share-hero-manage')).toBeInTheDocument()
    expect(screen.getByTestId('share-hero-link')).toHaveValue('http://100.64.0.1:5476/')
    expect(screen.getByTestId('share-hero-copy-message')).toHaveTextContent('Copy Tailscale invite')
    expect(screen.queryByTestId('share-hero-copy')).toBeNull()
    // No numbered steps and no step prose.
    expect(hero.querySelector('ol')).toBeNull()
    expect(hero.textContent).not.toMatch(/Share this machine on Tailscale|Send them the Crew link/)
    expect(hero.querySelector('details')?.textContent).toContain('crew.tail1234.ts.net')
  })

  it('opens no invite form until Invite… is chosen, and then only inside the dialog', async () => {
    await mountOwner()
    expect(screen.queryByTestId('invite-form')).toBeNull()
    fireEvent.click(screen.getByTestId('share-hero-invite'))
    const dialog = await screen.findByTestId('invite-dialog')
    expect(within(dialog).getByTestId('invite-form')).toBeInTheDocument()
    expect(screen.getAllByTestId('invite-form')).toHaveLength(1)
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
    // A link names nobody, so it is held in the invite dialog and not listed as a person.
    fireEvent.click(await screen.findByTestId('share-hero-invite', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS }))
    await screen.findByTestId('invite-l1')
    expect(screen.queryByTestId('people-list')?.querySelector('[data-testid="invite-l1"]') ?? null).toBeNull()
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

  it('labels the owner row "You · Owner" and a never-seen sharee "Sent"; others never seen carry no narration', async () => {
    await mountOwner()
    const me = screen.getByTestId('person-p_me')
    expect(me).toHaveTextContent('You · Owner')
    expect(me).toHaveTextContent('Here now')
    expect(screen.getByTestId('person-p_s')).toHaveTextContent('Sent')
    const cleo = screen.getByTestId('person-p_c')
    expect(cleo).not.toHaveTextContent('Sent')
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

  it('names an owner known only by a machine name "Crew owner", with the machine name as its detail', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    const host = row('p_o', 'build-host-7', { is_owner: true })
    host.principal.display.login = ''
    people.mockResolvedValue({ ...EMPTY, people: [host, row('p_me', 'Pat', { is_viewer: true })] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    const owner = await screen.findByTestId('person-p_o')
    expect(within(owner).getByTitle('Crew owner')).toBeInTheDocument()
    expect(within(owner).getByTestId('person-activity')).toHaveTextContent('build-host-7')
    expect(within(owner).getAllByText('Crew owner')).toHaveLength(1)
  })

  it('shows a teammate only the sources that are on, never Tailscale "Not set up", which only the owner can change', async () => {
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(false)
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_o', 'Owner', { is_owner: true }), row('p_me', 'Pat', { is_viewer: true, provenance: 'kiro_relay' })] })
    modeApi.mockResolvedValue(modeBody('network', {
      adapters: { tailscale: { state: 'unavailable', reason: 'not_configured' } },
      sources: [
        { id: 'tailscale', state: 'unavailable', reason: 'not_configured', people: 0 },
        { id: 'ssm', state: 'unavailable', reason: 'not_configured', people: 0 },
        { id: 'organization', state: 'unavailable', reason: 'not_offered', people: 0 },
        { id: 'kiro_login', state: 'available', reason: '', people: 1 },
      ],
    }))
    mount()
    expect(await screen.findByTestId('source-kiro_login')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('source-tailscale')).toBeNull())
    expect(screen.queryByTestId('source-ssm')).toBeNull()
    expect(screen.queryByText('Not set up')).toBeNull()
    expect(within(screen.getByTestId('person-p_me')).getByTestId('person-source')).toHaveTextContent('Kiro Login')
  })

  it('says nothing about per-person work when it is switched off, not failed', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, usage_off: true, people: [row('p_a', 'Ann')] })
    mount()
    expect(await screen.findByTestId('people-list')).toBeInTheDocument()
    expect(screen.queryByTestId('people-note')).toBeNull()
  })

  it('keeps Advanced reachable for an owner whose only answered read is SSM, still switched off', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available' })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_o', 'Mac Studio', { is_owner: true, is_viewer: true })] })
    modeApi.mockResolvedValue(modeBody('personal'))
    invitesApi.mockResolvedValue(null as never)
    vi.mocked(api.sharedCrewSsm).mockResolvedValue({ state: 'off', reason: 'disabled', port: 5477, target: null, command: '', local_url: '', invites: [] } as never)
    mount()
    // The switch that turns SSM on lives in Advanced, so it must show before SSM is on.
    expect(await screen.findByTestId('people-advanced', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.queryByTestId('share-hero-invite')).toBeNull()
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
    // The SSM switch names its outcome and says where the result shows up.
    expect(advanced).toHaveTextContent("Restart to turn this on. Invite then has an AWS SSM tab.")
    fireEvent.click(screen.getByRole('switch', { name: 'Let teammates join over AWS SSM' }))
    await waitFor(() => expect(patchApi).toHaveBeenCalledWith('dashboard.shared_crew.ssm', true))
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

  it('keeps a typed number in its field when the save fails, and clears it once a save lands', async () => {
    configApi.mockResolvedValue({ dashboard: { shared_crew: { principal_concurrency: 2 } } } as never)
    patchApi.mockRejectedValueOnce(new Error('nope'))
    await mountOwner()
    const input = await screen.findByTestId('people-advanced-principal_concurrency')
    await waitFor(() => expect(input).toHaveValue(2))

    fireEvent.change(input, { target: { value: '6' } })
    fireEvent.blur(input)
    expect(await screen.findByTestId('people-advanced-save-error')).toHaveAttribute('role', 'alert')
    await waitFor(() => expect(input).not.toBeDisabled())
    // The failed number is still there to retry, not the stored 2.
    expect(input).toHaveValue(6)

    patchApi.mockResolvedValueOnce({} as never)
    configApi.mockResolvedValue({ dashboard: { shared_crew: { principal_concurrency: 6 } } } as never)
    fireEvent.blur(input)
    await waitFor(() => expect(patchApi).toHaveBeenLastCalledWith('dashboard.shared_crew.principal_concurrency', 6))
    await waitFor(() => expect(input).not.toBeDisabled())
    expect(input).toHaveValue(6)
  })

  it('drops a failed number once another control saves, so it never shows without its error', async () => {
    configApi.mockResolvedValue({ dashboard: { shared_crew: { principal_concurrency: 2, admission_enabled: false } } } as never)
    patchApi.mockRejectedValueOnce(new Error('nope'))
    await mountOwner()
    const input = await screen.findByTestId('people-advanced-principal_concurrency')
    await waitFor(() => expect(input).toHaveValue(2))
    fireEvent.change(input, { target: { value: '6' } })
    fireEvent.blur(input)
    expect(await screen.findByTestId('people-advanced-save-error')).toBeInTheDocument()
    await waitFor(() => expect(input).not.toBeDisabled())
    expect(input).toHaveValue(6)

    patchApi.mockResolvedValueOnce({} as never)
    fireEvent.click(screen.getByRole('switch', { name: 'Fair admission' }))
    await waitFor(() => expect(patchApi).toHaveBeenLastCalledWith('dashboard.shared_crew.admission_enabled', true))
    await waitFor(() => expect(screen.queryByTestId('people-advanced-save-error')).toBeNull())
    // The number that never saved is gone with its error; the stored 2 shows.
    expect(screen.getByTestId('people-advanced-principal_concurrency')).toHaveValue(2)
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

describe('People: one row per person, one invite entry', () => {
  const EMPTY: PeopleResponse = { schema_version: 1, directory: { state: 'unavailable' }, usage_available: false, people: [] }
  const DIRECT = {
    state: 'active' as const, reason: '', method: 'localapi_unix', owner_login: 'owner@example.test',
    urls: ['http://100.64.0.1:5476/'], addresses: ['100.64.0.1'],
  }
  const invite = (id: string, over: Record<string, unknown> = {}) => ({
    id, transport: 'tailscale', kind: 'email' as const, email: 'a@example.test', multi_use: false, alias: '', avatar_id: '',
    created_at: 1_700_000_000, status: 'pending' as const, accepted_login: '', ...over,
  })
  const connected = (invites: unknown[]) => ({
    connected: true, tailnet: 'example', key_expires: '', key_expires_soon: false, key_expired: false, admin_share_url: '', invites,
  })
  const kyleLogin = { principal: { id: 'p_k', kind: 'human' as const, display: { name: 'Kyle', login: 'kyleseaman@gmail.com', captured_at: null } } }
  /** Kyle, connected but not admitted by any invite. */
  const kyleSeen = () => row('p_k', 'Kyle', kyleLogin)
  /** Kyle, a current member: the invite he came in by is bound to his row. */
  const kyleMember = (inviteId = 'i0') => row('p_k', 'Kyle', { ...kyleLogin, member: { alias: '', avatar_id: '', profile_pic_url: '', invite_id: inviteId } })
  const owner = () => row('p_o', 'Owner', { is_viewer: true, is_owner: true })

  async function mountNetwork(rows: PeopleResponse['people'], invites: unknown[], qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: rows })
    modeApi.mockResolvedValue(modeBody('network'))
    invitesApi.mockResolvedValue(connected(invites) as never)
    render(<QueryClientProvider client={qc}><PeoplePanel /></QueryClientProvider>)
    await screen.findByTestId('share-hero-invite', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })
    return qc
  }

  it('lists a pending invite the server still holds for a current member, and it can be removed', async () => {
    const revokeApi = vi.mocked(api.tailscaleRevokeInvite)
    revokeApi.mockResolvedValue({ revoked: 'i1', member_removed: false })
    await mountNetwork([owner(), kyleMember()], [invite('i1', { email: 'KyleSeaman@Gmail.com' })])
    await screen.findByTestId('person-p_k')
    // The server deletes a superseded invite in sync; one that is still returned is live.
    const pending = await screen.findByTestId('invite-i1')
    expect(screen.getByText('People · 2')).toBeInTheDocument()
    fireEvent.click(within(pending).getByTestId('invite-revoke'))
    fireEvent.click(within(pending).getByTestId('invite-revoke-confirm'))
    await waitFor(() => expect(revokeApi).toHaveBeenCalledWith('i1'))
  })

  it('keeps a live invite listed when its address is only a seen or named row, and counts people only', async () => {
    await mountNetwork(
      [owner(), kyleSeen(), row('p_s', 'Sam', { sources: ['tailscale_share' as const], principal: { id: 'p_s', kind: 'human' as const, display: { name: 'Sam', login: 'sam@example.test', captured_at: null } } })],
      [invite('i1', { email: 'kyleseaman@gmail.com' }), invite('i2', { email: 'SAM@example.test' }), invite('i3', { email: 'new@example.test' })],
    )
    for (const id of ['i1', 'i2', 'i3']) expect(within(await screen.findByTestId(`invite-${id}`)).getByText('Sent')).toBeInTheDocument()
    expect(screen.getByText('People · 3')).toBeInTheDocument()
    // The invites are rows of the one list, never counted in People · N.
    expect(within(screen.getByTestId('people-list')).getAllByRole('listitem')).toHaveLength(6)
  })

  it('turns an Invites row into the person once they are a member', async () => {
    const qc = await mountNetwork([owner()], [invite('i1', { email: 'kyleseaman@gmail.com' })])
    expect(await screen.findByTestId('invite-i1')).toBeInTheDocument()
    expect(screen.getByText('People · 1')).toBeInTheDocument()
    people.mockResolvedValue({ ...EMPTY, people: [owner(), kyleMember('i1')] })
    invitesApi.mockResolvedValue(connected([invite('i1', { email: 'kyleseaman@gmail.com', status: 'accepted', accepted_login: 'kyleseaman@gmail.com' })]) as never)
    await act(async () => { await qc.refetchQueries({ queryKey: sharedCrewKeys.people }); await qc.refetchQueries({ queryKey: sharedCrewKeys.all }) })
    await screen.findByTestId('person-p_k')
    expect(screen.queryByTestId('invite-i1')).toBeNull()
    expect(screen.getByText('People · 2')).toBeInTheDocument()
  })

  it('refreshes People when a poll shows an invite accepted, so the person is there when the invite row goes', async () => {
    const qc = await mountNetwork([owner()], [invite('i1', { email: 'kyleseaman@gmail.com' })])
    expect(await screen.findByTestId('invite-i1')).toBeInTheDocument()
    const reads = people.mock.calls.length
    invitesApi.mockResolvedValue(connected([invite('i1', { email: 'kyleseaman@gmail.com', status: 'accepted', accepted_login: 'kyleseaman@gmail.com' })]) as never)
    people.mockResolvedValue({ ...EMPTY, people: [owner(), kyleMember('i1')] })
    await act(async () => { await qc.refetchQueries({ queryKey: tailscaleInvitesKey }) })
    await screen.findByTestId('person-p_k')
    expect(people.mock.calls.length).toBeGreaterThan(reads)
    expect(screen.queryByTestId('invite-i1')).toBeNull()
  })

  it('does not list a reusable link as a person; the dialog holds it', async () => {
    await mountNetwork([owner(), kyleSeen()], [invite('l1', { kind: 'link', email: '', multi_use: true, status: 'accepted' })])
    await screen.findByTestId('person-p_k')
    expect(screen.queryByTestId('invite-l1')).toBeNull()
    expect(screen.getByText('People · 2')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('share-hero-invite'))
    expect(await within(await screen.findByTestId('invite-links')).findByTestId('invite-l1')).toHaveTextContent('Reusable link')
  })

  it('offers the owner Remove on a person who came in by a single-use invite', async () => {
    const revokeApi = vi.mocked(api.tailscaleRevokeInvite)
    revokeApi.mockResolvedValue({ revoked: 'i0', member_removed: true })
    await mountNetwork([owner(), kyleMember('i0')], [invite('i0', { status: 'accepted', accepted_login: 'kyleseaman@gmail.com', email: 'kyleseaman@gmail.com' })])
    const kyle = await screen.findByTestId('person-p_k')
    expect(within(kyle).queryByTestId('person-remove')).toBeNull()
    expect(within(kyle).getByTestId('invite-revoke')).toHaveTextContent('Remove')
    expect(kyle).not.toHaveTextContent('Revoke')
    fireEvent.click(within(kyle).getByTestId('invite-revoke'))
    expect(revokeApi).not.toHaveBeenCalled()
    expect(within(kyle).getByTestId('invite-revoke-confirm')).toHaveTextContent('Yes, remove')
    fireEvent.click(within(kyle).getByTestId('invite-revoke-confirm'))
    await waitFor(() => expect(revokeApi).toHaveBeenCalledWith('i0'))
    expect(within(screen.getByTestId('person-p_o')).queryByTestId('invite-revoke')).toBeNull()
  })

  it('links a member who came in by a reusable link to the Tailscale users page, never revoking the link', async () => {
    await mountNetwork(
      [owner(), row('p_m', 'Mia', { principal: { id: 'p_m', kind: 'human' as const, display: { name: 'Mia', login: 'mia@example.test', captured_at: null } }, member: { alias: '', avatar_id: '', profile_pic_url: '', invite_id: 'l1' } })],
      [invite('l1', { kind: 'link', email: '', multi_use: true, status: 'accepted' })],
    )
    const mia = await screen.findByTestId('person-p_m')
    const link = within(mia).getByTestId('person-remove')
    expect(link).toHaveAttribute('href', 'https://login.tailscale.com/admin/users')
    expect(link).toHaveTextContent('Manage in Tailscale')
    expect(within(mia).queryByTestId('invite-revoke')).toBeNull()
    expect(within(screen.getByTestId('person-p_o')).queryByTestId('person-remove')).toBeNull()
  })

  it('offers no Remove on a row that is not someone Tailscale shares with: merely seen, a session creator, or only named by a display record', async () => {
    await mountNetwork([owner(), kyleSeen(), row('p_c', 'Cleo', { sources: ['session_creator' as const], last_seen_at: null }), row('p_d', 'Dee', { member: { alias: 'Dee', avatar_id: '', profile_pic_url: '' } })], [])
    await screen.findByTestId('person-p_k')
    expect(screen.queryByTestId('person-remove')).toBeNull()
    expect(screen.queryByTestId('invite-revoke')).toBeNull()
  })

  it('names the person in the Remove confirm, in plain consequences', async () => {
    await mountNetwork([owner(), kyleMember('i0')], [invite('i0', { status: 'accepted', accepted_login: 'kyleseaman@gmail.com', email: 'kyleseaman@gmail.com' })])
    const kyle = await screen.findByTestId('person-p_k')
    fireEvent.click(within(kyle).getByTestId('invite-revoke'))
    expect(within(kyle).getByText('Kyle loses access.')).toBeInTheDocument()
    expect(kyle).not.toHaveTextContent('Access follows Tailscale rules')
  })

  it('keeps an accepted share removable in the list until its person has a row', async () => {
    const qc = await mountNetwork([owner()], [invite('i0', { status: 'accepted', accepted_login: 'kyleseaman@gmail.com', email: 'kyleseaman@gmail.com' })])
    const share = await screen.findByTestId('invite-i0')
    expect(share).toHaveTextContent('kyleseaman@gmail.com')
    expect(share).toHaveTextContent('Accepted, not connected yet')
    expect(within(share).queryByText('Sent')).toBeNull()
    expect(share).toHaveTextContent('Remove')
    expect(screen.getByText('People · 1')).toBeInTheDocument()
    people.mockResolvedValue({ ...EMPTY, people: [owner(), kyleMember('i0')] })
    await act(async () => { await qc.refetchQueries({ queryKey: sharedCrewKeys.people }) })
    await screen.findByTestId('person-p_k')
    expect(screen.queryByTestId('invite-i0')).toBeNull()
    expect(within(screen.getByTestId('person-p_k')).getByTestId('invite-revoke')).toBeInTheDocument()
  })

  it('offers a teammate no Remove', async () => {
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: [row('p_o', 'Owner', { is_owner: true }), row('p_me', 'Pat', { is_viewer: true }), kyleSeen()] })
    modeApi.mockResolvedValue(modeBody('network'))
    mount()
    await screen.findByTestId('person-p_k')
    expect(screen.queryByTestId('person-remove')).toBeNull()
    expect(screen.queryByTestId('invite-revoke')).toBeNull()
  })

  it('says when the person who got in used a different address than the one invited', async () => {
    await mountNetwork(
      [owner(), kyleMember('i0')],
      [invite('i0', { email: 'kyle@work.example', status: 'accepted', accepted_login: 'kyleseaman@gmail.com' })],
    )
    expect(await screen.findByTestId('person-other-account')).toHaveTextContent('Invited as kyle@work.example, accepted with another account.')
  })

  it('stays quiet when they came in under the address invited', async () => {
    await mountNetwork(
      [owner(), kyleMember('i0')],
      [invite('i0', { email: 'KyleSeaman@gmail.com', status: 'accepted', accepted_login: 'kyleseaman@gmail.com' })],
    )
    await screen.findByTestId('person-p_k')
    expect(screen.queryByTestId('person-other-account')).toBeNull()
  })

  it('keeps the edit button out of the row while the Remove confirm shows, so a row never holds three buttons', async () => {
    await mountNetwork(
      [owner(), { ...kyleMember('i0'), tailscale_user_id: '2' }],
      [invite('i0', { status: 'accepted', accepted_login: 'kyleseaman@gmail.com', email: 'kyleseaman@gmail.com' })],
    )
    const kyle = await screen.findByTestId('person-p_k')
    expect(within(kyle).getByTestId('person-edit')).toBeInTheDocument()
    fireEvent.click(within(kyle).getByTestId('invite-revoke'))
    expect(within(kyle).queryByTestId('person-edit')).toBeNull()
    expect(within(kyle).getAllByRole('button')).toHaveLength(2)
  })

  it('personal mode lists an unused email invite in the People list, still removable', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    invitesApi.mockResolvedValue(connected([invite('i1', { email: 'new@example.test' }), invite('l1', { kind: 'link', email: '' })]) as never)
    mount()
    const waiting = await screen.findByTestId('people-list', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })
    expect(within(within(waiting).getByTestId('invite-i1')).getByText('Sent')).toBeInTheDocument()
    expect(within(waiting).getByTestId('invite-i1')).toHaveTextContent('Remove')
    expect(within(waiting).queryByTestId('invite-l1')).toBeNull()
  })

  it('has one Invite… entry on the page, and the form only in its dialog', async () => {
    await mountNetwork([owner(), kyleMember()], [invite('i2', { email: 'qq@example.test' })])
    expect(screen.getAllByRole('button', { name: /^Invite…/ })).toHaveLength(1)
    expect(screen.queryByTestId('tailscale-invites')).toBeNull()
    expect(screen.queryByTestId('invite-form')).toBeNull()
  })

  it('a Crew in personal mode offers the same single Invite… entry', async () => {
    caps.mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available' })
    people.mockResolvedValue(EMPTY)
    invitesApi.mockResolvedValue(connected([]) as never)
    mount()
    expect(await screen.findByTestId('share-hero-invite', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getAllByTestId('share-hero-invite')).toHaveLength(1)
    expect(screen.queryByTestId('tailscale-invites')).toBeNull()
    expect(screen.queryByTestId('invite-form')).toBeNull()
    fireEvent.click(screen.getByTestId('share-hero-invite'))
    expect(await screen.findByTestId('invite-form')).toBeInTheDocument()
  })
})

describe('People: connection sources at once, one list with source chips', () => {
  const EMPTY: PeopleResponse = { schema_version: 1, directory: { state: 'unavailable' }, usage_available: true, people: [] }
  const DIRECT = {
    state: 'active' as const, reason: '', method: 'localapi_unix', owner_login: 'owner@example.test',
    urls: ['http://crew.tail1234.ts.net:5476/'], addresses: ['100.64.0.1'],
  }
  const source = (id: 'tailscale' | 'ssm' | 'organization' | 'kiro_login', state: 'available' | 'unavailable', people = 0) =>
    ({ id, state, reason: state === 'available' ? '' : 'not_configured', people })
  const ALL_ON = [source('tailscale', 'available', 2), source('ssm', 'available', 1), source('organization', 'unavailable'), source('kiro_login', 'available', 1)]
  const SSM_ACTIVE = {
    state: 'active', reason: '', port: 5476, target: { instance_id: 'i-0abc', region: 'us-west-2' }, command: 'aws ssm start-session', local_url: 'http://localhost:5476/',
    invites: [
      { id: 's_open', alias: 'Lee', created_at: 1_700_000_000, joined: true, principal_id: 'p_l' },
      { id: 's_new', alias: 'devon', created_at: 1_700_000_000, joined: false, principal_id: '' },
    ],
  }
  const tsInvite = (id: string, over: Record<string, unknown> = {}) => ({
    id, transport: 'tailscale', kind: 'email' as const, email: 'x@example.test', multi_use: false, alias: '', avatar_id: '',
    created_at: 1_700_000_000, status: 'pending' as const, accepted_login: '', ...over,
  })
  const connected = (invites: unknown[]) => ({
    connected: true, tailnet: 'example', key_expires: '', key_expires_soon: false, key_expired: false, admin_share_url: '', invites,
  })
  const person = (id: string, login: string, over: Partial<PeopleResponse['people'][number]> = {}) =>
    row(id, login, { principal: { id, kind: 'human' as const, display: { name: login, login, captured_at: null } }, usage: null, ...over })
  const ROWS = () => [
    person('p_o', 'owner@example.test', { is_viewer: true, is_owner: true, provenance: 'local_operator' }),
    person('p_t1', 'priya@example.test', { provenance: 'tailscale_serve', member: { alias: '', avatar_id: '', profile_pic_url: '', invite_id: 'i0' } }),
    person('p_t2', 'sam@example.test', { provenance: 'tailscale_listener', sources: ['tailscale_share' as const], last_seen_at: null }),
    person('p_l', 'lee@example.test', { provenance: 'ssm_invite' }),
    person('p_r', 'PRIYA@example.test', { provenance: 'kiro_relay' }),
    person('p_g', 'jordan@example.test', { sources: ['directory' as const] }),
  ]

  function mountAll({ teammate = false, sources = ALL_ON, ssm = SSM_ACTIVE as unknown, invites = [tsInvite('i0', { status: 'accepted', accepted_login: 'priya@example.test', email: 'priya@example.test' }), tsInvite('i9', { email: 'new@example.test' })] as unknown[] } = {}) {
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(!teammate)
    caps.mockResolvedValue({ ...SHARED, attribution: 'available', tailnet_direct: DIRECT })
    people.mockResolvedValue({ ...EMPTY, people: ROWS() })
    modeApi.mockResolvedValue(modeBody('network', { sources }))
    invitesApi.mockResolvedValue(teammate ? null : connected(invites) as never)
    vi.mocked(api.sharedCrewSsm).mockResolvedValue(teammate ? null : ssm as never)
    mount()
  }

  it('shows every source that is on side by side, each with its status and how many came in', async () => {
    mountAll()
    const tailscale = await screen.findByTestId('source-tailscale')
    expect(tailscale).toHaveAttribute('data-state', 'on')
    expect(tailscale).not.toHaveClass('border-dashed')
    expect(within(tailscale).getByTestId('source-tailscale-status')).toHaveTextContent('Connected')
    expect(within(tailscale).getByTestId('source-tailscale-body')).toHaveTextContent('Reachable at crew.tail1234.ts.net')
    expect(within(tailscale).getByTestId('source-tailscale-people')).toHaveTextContent('People: 2')
    const ssm = screen.getByTestId('source-ssm')
    expect(within(ssm).getByTestId('source-ssm-people')).toHaveTextContent('People: 1')
    expect(await within(ssm).findByText(/Listening for SSM port forwards on port 5476/)).toBeInTheDocument()
    expect(screen.getByTestId('source-kiro_login')).toHaveTextContent('Sharing is managed outside Kiro Crew.')
    // The organization console is not offered, so there is no card for it.
    expect(screen.queryByTestId('source-organization')).toBeNull()
  })

  it('gives each source the owner manages its own invite button, opening the dialog on that source', async () => {
    mountAll()
    const tsButton = await screen.findByTestId('share-hero-invite', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })
    expect(screen.getByTestId('source-tailscale')).toContainElement(tsButton)
    expect(tsButton).toHaveTextContent('Invite…')
    const ssmButton = await screen.findByTestId('source-ssm-invite')
    expect(screen.getByTestId('source-ssm')).toContainElement(ssmButton)
    expect(ssmButton).toHaveTextContent('Make invite link')
    // Kiro Login sharing is managed outside Kiro Crew: no invite button.
    expect(within(screen.getByTestId('source-kiro_login')).queryAllByRole('button')).toHaveLength(0)
    fireEvent.click(ssmButton)
    const dialog = await screen.findByTestId('invite-dialog')
    expect(within(dialog).getByTestId('invite-transport-ssm')).toHaveAttribute('aria-selected', 'true')
    expect(within(dialog).getByTestId('ssm-form')).toBeVisible()
  })

  it('locks the invite buttons for a teammate, saying only the owner can invite', async () => {
    mountAll({ teammate: true })
    const locked = await screen.findByTestId('source-tailscale-invite-locked')
    expect(locked).toBeDisabled()
    expect(locked).toHaveTextContent('Invite…')
    expect(screen.getByTestId('source-ssm-invite-locked')).toBeDisabled()
    expect(screen.getByTestId('source-ssm-invite-locked')).toHaveTextContent('Make invite link')
    expect(locked).toHaveAccessibleDescription('Only the owner of this Crew can invite people.')
    expect(screen.queryByTestId('share-hero-invite')).toBeNull()
    expect(screen.queryByTestId('source-ssm-invite')).toBeNull()
  })

  it('shows SSM not set up as a dashed card with its setup button, after the sources that are on', async () => {
    mountAll({ sources: [source('tailscale', 'available', 2), source('ssm', 'unavailable'), source('organization', 'unavailable'), source('kiro_login', 'unavailable')], ssm: { ...SSM_ACTIVE, state: 'off', invites: [] } })
    const ssm = await screen.findByTestId('source-ssm')
    expect(ssm).toHaveClass('border-dashed')
    expect(within(ssm).getByTestId('source-ssm-status')).toHaveTextContent('Not set up')
    expect(within(ssm).getByTestId('source-ssm-setup')).toHaveTextContent('Set up AWS SSM')
    expect(within(ssm).getByTestId('source-ssm-setup')).toHaveAttribute('href', expect.stringContaining('share-one-crew-over-aws-ssm'))
    const cards = within(screen.getByTestId('people-sources')).getAllByRole('listitem').map(li => li.getAttribute('data-testid'))
    expect(cards).toEqual(['source-tailscale', 'source-ssm'])
  })

  it('lists everyone in one list with the chip of the source they came in through, counting distinct people', async () => {
    mountAll()
    const list = await screen.findByTestId('people-list')
    const chip = (id: string) => within(screen.getByTestId(`person-${id}`)).queryByTestId('person-source')?.textContent ?? null
    expect(chip('p_t1')).toBe('Tailscale')
    expect(chip('p_t2')).toBe('Tailscale')
    expect(chip('p_l')).toBe('AWS SSM')
    expect(chip('p_r')).toBe('Kiro Login')
    expect(chip('p_g')).toBe('Your organization')
    // The owner at this machine came in through no source.
    expect(chip('p_o')).toBeNull()
    expect(within(screen.getByTestId('person-p_o')).getByText('You · Owner')).toBeInTheDocument()
    // Priya is on Tailscale and on Kiro Login: two rows, one person.
    expect(screen.getByText('People · 5')).toBeInTheDocument()
    expect(screen.getByTestId('people-list-note')).toHaveTextContent('The same person can appear once for each source they came in through.')
    // Invites nobody used are rows of the same list, with their chip, and not counted.
    const ssmInvite = await within(list).findByTestId('ssm-invite-s_new')
    expect(within(ssmInvite).getByTestId('person-source')).toHaveTextContent('AWS SSM')
    expect(within(within(list).getByTestId('invite-i9')).getByTestId('person-source')).toHaveTextContent('Tailscale')
  })

  it('puts Remove only where it works, a Tailscale link everywhere else, and nothing on organization or Kiro Login people', async () => {
    mountAll()
    await screen.findByTestId('ssm-invite-s_new', {}, { timeout: OWNER_CHAIN_TIMEOUT_MS })
    await screen.findByTestId('invite-i9')
    const rowOf = (id: string) => screen.getByTestId(`person-${id}`)
    // A person of a single-use Tailscale invite: Remove revokes it.
    expect(within(rowOf('p_t1')).getByTestId('invite-revoke')).toHaveTextContent('Remove')
    // Any other Tailscale person: managed in Tailscale.
    expect(within(rowOf('p_t2')).getByTestId('person-remove')).toHaveTextContent('Manage in Tailscale')
    expect(within(rowOf('p_t2')).getByTestId('person-remove')).toHaveAttribute('href', 'https://login.tailscale.com/admin/users')
    // An SSM person: Remove.
    expect(within(rowOf('p_l')).getByTestId('invite-revoke')).toHaveTextContent('Remove')
    // An unopened SSM invite: Revoke invite.
    expect(within(screen.getByTestId('ssm-invite-s_new')).getByTestId('invite-revoke')).toHaveTextContent('Revoke invite')
    // A single-use Tailscale invite row: Remove.
    expect(within(screen.getByTestId('invite-i9')).getByTestId('invite-revoke')).toHaveTextContent('Remove')
    // Organization people are read-only; Kiro Login people are managed outside.
    expect(within(rowOf('p_g')).getByTestId('person-managed')).toHaveTextContent('Managed by admin')
    expect(within(rowOf('p_g')).queryByRole('button')).toBeNull()
    expect(within(rowOf('p_r')).queryByRole('button')).toBeNull()
    expect(within(rowOf('p_r')).queryByRole('link')).toBeNull()
    expect(within(rowOf('p_o')).queryByTestId('invite-revoke')).toBeNull()
  })

  it('gives a teammate no Remove and no Tailscale link, and still names organization people read-only', async () => {
    mountAll({ teammate: true })
    await screen.findByTestId('person-p_g')
    expect(screen.queryByTestId('invite-revoke')).toBeNull()
    expect(screen.queryByTestId('person-remove')).toBeNull()
    expect(within(screen.getByTestId('person-p_g')).getByTestId('person-managed')).toBeInTheDocument()
  })
})
