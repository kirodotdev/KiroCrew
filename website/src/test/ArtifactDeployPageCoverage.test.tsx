import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { useLocation } from 'react-router-dom'
import ArtifactDeployPage from '../pages/ArtifactDeployPage'
import { renderWithProviders } from './helpers'

/**
 * ArtifactDeployPage: the write paths.
 *
 * ArtifactDeployPage.test.tsx covers what the console RENDERS from its three
 * read queries. This file covers what it SENDS: the profile registry mutations,
 * the IAM-policy loader, the two-call recall/destroy guard (preview → confirm
 * bound to the previewed resources), and the pending-confirmation card.
 *
 * The page talks to the deploy backend through raw `fetch`, so every test drives
 * one stub that routes on URL + method and can be told which status to answer
 * with — that is the only way to reach the `status >= 400` notice branches.
 */

type Json = Record<string, unknown>

interface FetchCfg {
  profiles?: Json[]
  defaultProfile?: string
  available?: string[]
  sites?: Json[]
  webapps?: Json[]
  pending?: Json[]
  policy?: Json
  verify?: Json
  /** answer for POST / PUT / DELETE on the profile registry */
  write?: { status: number; body: Json }
  /** answer for the FIRST (preview) call of the recall/destroy guard */
  preview?: { status: number; body: Json }
  /** answer for the SECOND (confirm: true) call */
  commit?: { status: number; body: Json }
  confirmPending?: { status: number; body: Json }
  dismissPending?: { status: number; body: Json }
}

interface Call { url: string; method: string; body: Json | null }

const PROFILES: Json[] = [
  { name: 'ship-prod', region: 'us-west-2', account: '123456789012', verified_at: '2026-07-14T00:00:00+00:00', note: '' },
  { name: 'ship-sandbox', region: 'us-east-1', account: '', verified_at: '', note: '' },
]

const SITE: Json = {
  site_id: 'blog', bucket: 'b', distribution_id: 'd',
  status: 'deployed', url: 'https://d111.cloudfront.net', profile: 'ship-prod',
}

function installFetch(cfg: FetchCfg = {}): Call[] {
  const calls: Call[] = []
  const reply = (status: number, data: Json) =>
    ({ ok: status < 400, status, json: async () => data }) as unknown as Response
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    const method = (init?.method ?? 'GET').toUpperCase()
    const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null
    calls.push({ url: u, method, body })
    if (u.startsWith('/api/artifacts')) return reply(200, { artifacts: cfg.webapps ?? [] })
    if (u.endsWith('/list')) return reply(200, { sites: cfg.sites ?? [], configured: true })
    if (u.endsWith('/pending')) return reply(200, { pending: cfg.pending ?? [] })
    if (u.endsWith('/confirm')) return reply(cfg.confirmPending?.status ?? 200, cfg.confirmPending?.body ?? { ok: true })
    if (u.endsWith('/dismiss')) return reply(cfg.dismissPending?.status ?? 200, cfg.dismissPending?.body ?? { ok: true })
    if (u.includes('/iam-policy')) return reply(200, cfg.policy ?? { policy: 'STATIC-POLICY-JSON' })
    if (u.endsWith('/verify')) {
      return reply(200, cfg.verify ?? {
        reachable: true, profile: 'ship-prod', account: '123456789012', note: 'sts + s3 + cloudfront reachable',
      })
    }
    if (u.endsWith('/recall') || u.endsWith('/destroy')) {
      return body?.confirm
        ? reply(cfg.commit?.status ?? 200, cfg.commit?.body ?? { ok: true })
        : reply(cfg.preview?.status ?? 200, cfg.preview?.body ?? { resources: { bucket: 'bkt-9f3', distribution_id: 'E1DIST' } })
    }
    if (u.includes('/profiles')) {
      return method === 'GET'
        ? reply(200, {
            profiles: cfg.profiles ?? PROFILES,
            default: cfg.defaultProfile ?? 'ship-prod',
            available: cfg.available ?? [],
          })
        : reply(cfg.write?.status ?? 200, cfg.write?.body ?? { ok: true })
    }
    return reply(200, {})
  })
  vi.stubGlobal('fetch', fn)
  return calls
}

/** Stub the clipboard on the REAL navigator — spreading it into a fake drops
 *  every prototype accessor the render tree reads (userAgent, language). */
function installClipboard() {
  const writeText = vi.fn()
  Object.defineProperty(window.navigator, 'clipboard', { value: { writeText }, configurable: true })
  return writeText
}

function webapp(slug: string, over: { url?: string; status?: string; estimates?: Json[] } = {}): Json {
  return {
    slug,
    name: slug,
    kind: 'webapp',
    source: 'chat',
    description: '',
    tags: [],
    version: 1,
    created_at: '2026-07-10T10:00:00Z',
    updated_at: '2026-07-10T10:00:00Z',
    webapp_metadata: {
      slug,
      deploy_target: { provider: 'aws', region: 'us-west-2', public_url: over.url ?? '', profile: 'ship-prod' },
      lifecycle: { persistent: true, ttl_hours: 72, status: over.status ?? 'draft' },
      cost: { model: 'ttl-window', estimates: over.estimates ?? [{ views: 100, usd: 0.02 }] },
    },
  }
}

function pendingEntry(over: Json = {}): Json {
  return {
    id: 'p1',
    site_id: 'kanban-preview',
    artifact_slug: 'kanban',
    local_dir: '',
    profile: 'ship-prod',
    region: 'us-west-2',
    ttl_hours: 24,
    scan_summary: 'clean',
    created_at_epoch: Math.floor(Date.now() / 1000) - 600,
    ...over,
  }
}

/** Renders the pathname so a navigation away from the console is observable. */
function LocationProbe() {
  const { pathname } = useLocation()
  return <span>route: {pathname}</span>
}

function renderPage() {
  return renderWithProviders(<><ArtifactDeployPage /><LocationProbe /></>)
}

const profilesLoaded = () => screen.findByText(/AWS Profiles \(2\)/)
const writes = (calls: Call[], fragment: string) =>
  calls.filter((c) => c.method !== 'GET' && c.url.includes(fragment))

describe('ArtifactDeployPage — navigation, disclosure, and copy affordances', () => {
  beforeEach(() => vi.unstubAllGlobals())
  afterEach(() => vi.restoreAllMocks())

  it('takes the Back control out of the console and into the gallery', async () => {
    installFetch()
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /Back to Artifacts/i }))
    expect(screen.getByText('route: /artifacts')).toBeInTheDocument()
  })

  it('collapses the getting-started guide, which starts expanded', async () => {
    installFetch()
    renderPage()
    expect(await screen.findByText('1. Authenticate to AWS')).toBeInTheDocument()
    fireEvent.click(screen.getByText(/Getting started \(one-time AWS setup\)/))
    expect(screen.queryByText('1. Authenticate to AWS')).toBeNull()
  })

  it('expands the security model, which starts collapsed', async () => {
    installFetch()
    renderPage()
    await profilesLoaded()
    expect(screen.queryByText('Your credentials never touch Kiro Crew.')).toBeNull()
    fireEvent.click(screen.getByText(/How this is secured/))
    expect(screen.getByText('Your credentials never touch Kiro Crew.')).toBeInTheDocument()
  })

  it('copies a setup command verbatim, comment and all', async () => {
    installFetch()
    const writeText = installClipboard()
    renderPage()
    const copyButtons = await screen.findAllByRole('button', { name: 'Copy' })
    expect(copyButtons).toHaveLength(2)
    fireEvent.click(copyButtons[0])
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('aws configure sso'))
  })

  it('states the empty case for both the registry and the deployment list', async () => {
    installFetch({ profiles: [], defaultProfile: '' })
    renderPage()
    expect(await screen.findByText(/No profiles yet/)).toBeInTheDocument()
    expect(screen.getByText(/No deployments yet/)).toBeInTheDocument()
    expect(screen.getByText(/AWS Profiles \(0\)/)).toBeInTheDocument()
  })

  it('re-queries the deployment sources when Refresh is pressed', async () => {
    const calls = installFetch({ sites: [SITE] })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    const before = calls.filter((c) => c.url.endsWith('/list')).length
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith('/list')).length).toBeGreaterThan(before))
  })
})

describe('ArtifactDeployPage — profile registry mutations', () => {
  beforeEach(() => vi.unstubAllGlobals())
  afterEach(() => vi.restoreAllMocks())

  it('registers a profile discovered in the AWS config in one click', async () => {
    const calls = installFetch({ available: ['other-sso'] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /other-sso/ }))
    expect(await screen.findByText("Registered profile 'other-sso'.")).toBeInTheDocument()
    const [post] = writes(calls, '/profiles')
    expect(post.method).toBe('POST')
    expect(post.body).toEqual({ name: 'other-sso', region: 'us-west-2' })
  })

  it('surfaces the backend reason a registration was refused', async () => {    installFetch({ available: ['other-sso'], write: { status: 400, body: { error: 'profile not found in ~/.aws/config' } } })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /other-sso/ }))
    expect(await screen.findByText('Error: profile not found in ~/.aws/config')).toBeInTheDocument()
  })

  it('falls back to a generic reason when the refusal carries no error field', async () => {
    installFetch({ available: ['other-sso'], write: { status: 500, body: {} } })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /other-sso/ }))
    expect(await screen.findByText('Error: add failed')).toBeInTheDocument()
  })

  it('creates and registers a profile from the form, then closes and clears it', async () => {
    const calls = installFetch()
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByRole('button', { name: /New profile/ }))

    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: '  fresh-sandbox  ' } })
    fireEvent.change(screen.getByLabelText('Region'), { target: { value: 'eu-central-1' } })
    // The account/role pair only exists once the caller opts into writing the
    // profile into ~/.aws/config.
    expect(screen.queryByLabelText(/^Account \(12 digits/)).toBeNull()
    fireEvent.click(screen.getByRole('switch', { name: 'Also create in AWS config' }))
    fireEvent.change(screen.getByLabelText(/^Account \(12 digits/), { target: { value: '210987654321' } })
    fireEvent.change(screen.getByLabelText('Role (optional)'), { target: { value: 'DeployRole' } })

    fireEvent.click(screen.getByRole('button', { name: 'Create + register' }))
    expect(await screen.findByText("Created + registered profile 'fresh-sandbox'.")).toBeInTheDocument()
    expect(writes(calls, '/profiles')[0].body).toEqual({
      name: 'fresh-sandbox', region: 'eu-central-1', create: true, account: '210987654321', role: 'DeployRole',
    })
    expect(screen.queryByLabelText('Profile name')).toBeNull()
  })

  it('defaults a blank region to us-west-2 and refuses a nameless profile', async () => {
    const calls = installFetch()
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByRole('button', { name: /New profile/ }))
    expect(screen.getByRole('button', { name: 'Register' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'no-region' } })
    fireEvent.change(screen.getByLabelText('Region'), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Register' }))
    await waitFor(() => expect(writes(calls, '/profiles')).toHaveLength(1))
    expect(writes(calls, '/profiles')[0].body).toMatchObject({ name: 'no-region', region: 'us-west-2', create: false })
  })

  it('promotes a profile to default with a PUT, and ignores a click on the current default', async () => {
    const calls = installFetch()
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByLabelText('ship-prod is the default profile'))
    expect(writes(calls, '/profiles')).toHaveLength(0)

    fireEvent.click(screen.getByLabelText('Make ship-sandbox the default profile'))
    await waitFor(() => expect(writes(calls, '/profiles')).toHaveLength(1))
    const [put] = writes(calls, '/profiles')
    expect(put.method).toBe('PUT')
    expect(put.url).toBe('/api/deploy/profiles/ship-sandbox')
    expect(put.body).toEqual({ default: true })
  })

  it('reports a rejected default change instead of silently keeping the old one', async () => {
    installFetch({ write: { status: 409, body: {} } })
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByLabelText('Make ship-sandbox the default profile'))
    expect(await screen.findByText('Error: update failed')).toBeInTheDocument()
  })

  it('removes a profile from the registry after confirmation, saying the AWS config is untouched', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const calls = installFetch()
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByLabelText('Remove ship-sandbox from registry'))
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("Remove 'ship-sandbox' from the registry?"))
    expect(await screen.findByText('Removed from registry (your ~/.aws/config is untouched).')).toBeInTheDocument()
    expect(writes(calls, '/profiles')[0].method).toBe('DELETE')
  })

  it('sends nothing when the removal confirm is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const calls = installFetch()
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByLabelText('Remove ship-sandbox from registry'))
    expect(writes(calls, '/profiles')).toHaveLength(0)
  })

  it('reports a failed removal', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    installFetch({ write: { status: 500, body: {} } })
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByLabelText('Remove ship-sandbox from registry'))
    expect(await screen.findByText('Error: remove failed')).toBeInTheDocument()
  })

  it('shows the account behind a verified profile', async () => {
    const calls = installFetch()
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getAllByRole('button', { name: /Verify/ })[0])
    // Built from the mock rather than inlined: the literal "account <12
    // digits>" string is the shape scripts/scrub-lint.sh rejects.
    const account = (PROFILES[0] as { account: string }).account
    expect(
      await screen.findByText(new RegExp(`access reachable \\(account ${account}\\)`)),
    ).toBeInTheDocument()
    expect(screen.getByText('sts + s3 + cloudfront reachable')).toBeInTheDocument()
    expect(writes(calls, '/verify')[0].body).toEqual({ profile: 'ship-prod' })
  })

  it('shows the failure detail when a profile is not reachable', async () => {
    installFetch({ verify: { reachable: false, detail: 'ExpiredToken: sso session expired', note: 'run aws sso login' } })
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getAllByRole('button', { name: /Verify/ })[0])
    expect(await screen.findByText('ExpiredToken: sso session expired')).toBeInTheDocument()
  })

  it('falls back to a plain not-reachable message when neither detail nor error is given', async () => {
    installFetch({ verify: { reachable: false, note: '' } })
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getAllByRole('button', { name: /Verify/ })[0])
    expect(await screen.findByText('not reachable')).toBeInTheDocument()
  })
})

describe('ArtifactDeployPage — IAM policy loader', () => {
  beforeEach(() => vi.unstubAllGlobals())
  afterEach(() => vi.restoreAllMocks())

  it('loads the static-tier policy and copies it, with no boundary block', async () => {
    const writeText = installClipboard()
    const calls = installFetch()
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'Get IAM policy' }))
    expect(await screen.findByText('STATIC-POLICY-JSON')).toBeInTheDocument()
    expect(calls.some((c) => c.url.includes('/iam-policy?tier=static'))).toBe(true)
    expect(screen.queryByRole('button', { name: /Copy boundary policy/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Copy policy/ }))
    expect(writeText).toHaveBeenCalledWith('STATIC-POLICY-JSON')
  })

  it('loads the fullstack tier with its permissions-boundary policy and note', async () => {
    const writeText = installClipboard()
    const calls = installFetch({
      policy: {
        policy: 'FULLSTACK-POLICY-JSON',
        boundary_policy: 'BOUNDARY-POLICY-JSON',
        boundary_policy_name: 'deploy-app-boundary',
        boundary_note: 'Create this boundary before the first deploy',
      },
    })
    renderPage()
    await profilesLoaded()

    fireEvent.click(screen.getByRole('combobox', { name: 'Policy tier' }))
    fireEvent.click(await screen.findByRole('option', { name: 'fullstack' }))
    fireEvent.click(screen.getByRole('button', { name: 'Get IAM policy' }))

    expect(await screen.findByText('FULLSTACK-POLICY-JSON')).toBeInTheDocument()
    expect(calls.some((c) => c.url.includes('/iam-policy?tier=fullstack'))).toBe(true)
    expect(screen.getByText(/Fullstack tier: includes Lambda/)).toBeInTheDocument()
    expect(screen.getByText('Create this boundary before the first deploy (name: deploy-app-boundary)')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Copy boundary policy/ }))
    expect(writeText).toHaveBeenCalledWith('BOUNDARY-POLICY-JSON')
  })

  it('explains the boundary requirement itself when the backend sends no note', async () => {
    installFetch({ policy: { policy: 'FULLSTACK-POLICY-JSON', boundary_policy: 'BOUNDARY-POLICY-JSON' } })
    renderPage()
    await profilesLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'Get IAM policy' }))
    expect(await screen.findByText('BOUNDARY-POLICY-JSON')).toBeInTheDocument()
    expect(screen.getByText(/Fullstack also requires the permissions-boundary policy below/)).toBeInTheDocument()
  })
})

describe('ArtifactDeployPage — recall and destroy two-call guard', () => {
  beforeEach(() => vi.unstubAllGlobals())
  afterEach(() => vi.restoreAllMocks())

  it('binds a confirmed recall to the resources the preview call resolved', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const calls = installFetch({ sites: [SITE] })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    fireEvent.click(screen.getByRole('button', { name: /Recall/ }))

    await waitFor(() => expect(writes(calls, '/recall')).toHaveLength(2))
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('bkt-9f3'))
    const [preview, commit] = writes(calls, '/recall')
    expect(preview.body).toEqual({ site_id: 'blog', profile: 'ship-prod' })
    expect(commit.body).toEqual({
      site_id: 'blog', confirm: true, profile: 'ship-prod',
      expected_bucket: 'bkt-9f3', expected_distribution_id: 'E1DIST',
    })
    expect(await screen.findByText("Recalled 'blog'.")).toBeInTheDocument()
  })

  it('sends no confirmed recall when the dialog is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const calls = installFetch({ sites: [SITE] })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    fireEvent.click(screen.getByRole('button', { name: /Recall/ }))
    await waitFor(() => expect(writes(calls, '/recall')).toHaveLength(1))
    expect(screen.queryByText(/Recalled/)).toBeNull()
  })

  it('never reaches the dialog when the recall preview itself fails', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const calls = installFetch({ sites: [SITE], preview: { status: 500, body: { error: 'no such site' } } })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    fireEvent.click(screen.getByRole('button', { name: /Recall/ }))
    await waitFor(() => expect(writes(calls, '/recall')).toHaveLength(1))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.queryByText(/Recalled/)).toBeNull()
  })

  it('reports a recall the backend refused after confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    installFetch({ sites: [SITE], commit: { status: 409, body: { error: 'bucket changed since preview' } } })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    fireEvent.click(screen.getByRole('button', { name: /Recall/ }))
    expect(await screen.findByText('Error: bucket changed since preview')).toBeInTheDocument()
  })

  it('names the bucket and the distribution in the destroy dialog and binds both', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const calls = installFetch({ sites: [SITE] })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    fireEvent.click(screen.getByRole('button', { name: /Destroy/ }))

    await waitFor(() => expect(writes(calls, '/destroy')).toHaveLength(2))
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('distribution E1DIST'))
    expect(writes(calls, '/destroy')[1].body).toMatchObject({
      confirm: true, expected_bucket: 'bkt-9f3', expected_distribution_id: 'E1DIST',
    })
    expect(await screen.findByText(/Destroying 'blog'/)).toBeInTheDocument()
  })

  it('marks unknown resources with a question mark rather than an empty dialog', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    installFetch({ sites: [SITE], preview: { status: 200, body: {} } })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    fireEvent.click(screen.getByRole('button', { name: /Destroy/ }))
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledWith(expect.stringMatching(/bucket \? and distribution \?/)))
  })

  it('reports a destroy that the backend refused after confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    installFetch({ sites: [SITE], commit: { status: 409, body: { error: 'site was recreated since preview' } } })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    fireEvent.click(screen.getByRole('button', { name: /Destroy/ }))
    expect(await screen.findByText('Error: site was recreated since preview')).toBeInTheDocument()
  })

  it('fails the destroy closed when the preview cannot resolve the resources', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const calls = installFetch({ sites: [SITE], preview: { status: 503, body: {} } })
    renderPage()
    await screen.findByText(/Deployments \(1\)/)
    fireEvent.click(screen.getByRole('button', { name: /Destroy/ }))
    // No dialog, no confirmed call: a destroy whose target could not be
    // resolved must not proceed unbound.
    await waitFor(() => expect(writes(calls, '/destroy')).toHaveLength(1))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.queryByText(/Destroying/)).toBeNull()
  })
})

describe('ArtifactDeployPage — deployment rows', () => {
  beforeEach(() => vi.unstubAllGlobals())
  afterEach(() => vi.restoreAllMocks())

  it('renders a non-http deployment URL as inert text, never as a link', async () => {
    installFetch({
      sites: [
        { site_id: 'spoofed', bucket: 'b', distribution_id: 'd', status: 'error', url: 'javascript:alert(1)', profile: '' },
        { site_id: 'stateless', bucket: 'b', distribution_id: 'd' },
      ],
    })
    renderPage()
    await screen.findByText(/Deployments \(2\)/)
    const unsafe = screen.getByText('javascript:alert(1)')
    expect(unsafe.closest('a')).toBeNull()
    // A site the backend has not classified yet reads as unknown, not as live.
    expect(screen.getByText('unknown')).toBeInTheDocument()
    expect(screen.getByText('error')).toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })

  it('omits the profile clause from the deploy handoff when nothing is registered', async () => {
    installFetch({ profiles: [], defaultProfile: '', webapps: [webapp('kanban-draft')] })
    renderPage()
    await screen.findByText(/Ready to deploy \(1\)/)
    // With no registered profile there is nothing to pick from, so no selector.
    expect(screen.queryByRole('combobox', { name: /Deploy profile/ })).toBeNull()
    fireEvent.click(screen.getByLabelText('Deploy kanban-draft'))
    const launch = (window as unknown as { __mc_chat_launch?: { message: string } }).__mc_chat_launch
    expect(launch?.message).toContain('kanban-draft')
    expect(launch?.message).not.toContain('Use the AWS profile')
    expect(screen.getByText('route: /chat')).toBeInTheDocument()
  })

  it('reads a costless draft as ~$0.00 and hides an expired one', async () => {
    installFetch({
      webapps: [
        webapp('free-draft', { estimates: [] }),
        webapp('gone-draft', { status: 'expired' }),
      ],
    })
    renderPage()
    await screen.findByText(/Ready to deploy \(1\)/)
    expect(screen.getByText('free-draft')).toBeInTheDocument()
    expect(screen.queryByText('gone-draft')).toBeNull()
    expect(screen.getByText('~$0.00')).toBeInTheDocument()
  })
})

describe('ArtifactDeployPage — pending confirmations', () => {
  beforeEach(() => vi.unstubAllGlobals())
  afterEach(() => vi.restoreAllMocks())

  it('stays hidden while nothing awaits a human', async () => {
    installFetch()
    renderPage()
    await profilesLoaded()
    expect(screen.queryByText(/Pending confirmations/)).toBeNull()
  })

  it('lists each waiting deploy with its source, profile, TTL, scan and age', async () => {
    installFetch({
      pending: [
        pendingEntry(),
        pendingEntry({
          id: 'p2', site_id: 'notes-preview', artifact_slug: '', local_dir: '', profile: '',
          ttl_hours: 6, scan_summary: '2 findings', override_scan_required: true,
          created_at_epoch: Math.floor(Date.now() / 1000) - 120,
        }),
      ],
    })
    renderPage()
    expect(await screen.findByText(/Pending confirmations \(2\)/)).toBeInTheDocument()
    expect(screen.getByText(/Source: kanban · Profile: ship-prod · TTL: 24h · Scan: clean · 10m ago/)).toBeInTheDocument()
    // Neither an artifact nor a local directory: the source is unknown, not blank.
    expect(screen.getByText(/Source: \(unknown\) · Profile: default · TTL: 6h/)).toBeInTheDocument()
    expect(screen.getByText(/Blocked by non-credential scan findings/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm Deploy' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Deploy anyway' })).toBeInTheDocument()
  })

  it('sends override_scan only for the entry the scan blocked', async () => {
    const calls = installFetch({
      pending: [
        pendingEntry(),
        pendingEntry({ id: 'p2', site_id: 'notes-preview', override_scan_required: true }),
      ],
    })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Deploy anyway' }))
    // #3599: the pending confirm commits through the acknowledgment dialog.
    fireEvent.click(
      await screen.findByRole('button', { name: 'I understand, publish publicly' }),
    )
    await waitFor(() => expect(writes(calls, '/pending/p2/confirm')).toHaveLength(1))
    expect(writes(calls, '/pending/p2/confirm')[0].body).toEqual({ override_scan: true })

    fireEvent.click(screen.getByRole('button', { name: 'Confirm Deploy' }))
    // #3599: the pending confirm commits through the acknowledgment dialog.
    fireEvent.click(
      await screen.findByRole('button', { name: 'I understand, publish publicly' }),
    )
    await waitFor(() => expect(writes(calls, '/pending/p1/confirm')).toHaveLength(1))
    expect(writes(calls, '/pending/p1/confirm')[0].body).toEqual({})
  })

  it('shows the backend reason a confirmed deploy was rejected', async () => {
    installFetch({ pending: [pendingEntry()], confirmPending: { status: 400, body: { error: 'credential finding still present' } } })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm Deploy' }))
    // #3599: the pending confirm commits through the acknowledgment dialog.
    fireEvent.click(
      await screen.findByRole('button', { name: 'I understand, publish publicly' }),
    )
    expect(await screen.findByText('credential finding still present')).toBeInTheDocument()
  })

  it('dismisses a waiting deploy', async () => {
    const calls = installFetch({ pending: [pendingEntry()] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(writes(calls, '/pending/p1/dismiss')).toHaveLength(1))
    expect(writes(calls, '/pending/p1/dismiss')[0].method).toBe('POST')
  })

  it('reports a failed dismiss with the status when the body carries no reason', async () => {
    installFetch({ pending: [pendingEntry()], dismissPending: { status: 500, body: {} } })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }))
    expect(await screen.findByText('Dismiss failed (500)')).toBeInTheDocument()
  })
})
