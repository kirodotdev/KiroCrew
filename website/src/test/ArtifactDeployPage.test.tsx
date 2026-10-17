import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactElement } from 'react'
import ArtifactDeployPage from '../pages/ArtifactDeployPage'

// ArtifactDeployPage talks to the app backend + core artifacts API via raw fetch —
// route responses by URL.
function mockFetch(webapps: unknown[], opts?: {
  profiles?: unknown[]; default?: string; available?: string[]
  /** Answer POST /api/deploy/deploy. Receives the parsed request body and
   *  returns the response body; the deploy flow is a two-call preview/confirm
   *  exchange, so a test scripts both from the `confirm` flag. Omit it and the
   *  endpoint keeps its previous inert reply. */
  onDeploy?: (body: Record<string, unknown>) => Record<string, unknown>
  /** HTTP status for the onDeploy reply (default 200) — lets a test drive the
   *  409/400 refusal affordances. */
  deployStatus?: number
}) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    if (opts?.onDeploy && u.endsWith('/deploy/deploy')) {
      const parsed = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {}
      const body = opts.onDeploy(parsed)
      const status = opts.deployStatus ?? 200
      return { ok: status < 400, status, json: async () => body } as unknown as Response
    }
    const body = u.includes('/api/artifacts')
      ? { artifacts: webapps }
      : u.endsWith('/list')
        ? { sites: [{ site_id: 'blog', bucket: 'b', distribution_id: 'd', status: 'Deployed', url: 'https://x.cloudfront.net', profile: 'my-deploy' }], configured: true }
        : u.endsWith('/profiles')
          ? {
              profiles: opts?.profiles ?? [
                { name: 'my-deploy', region: 'us-west-2', account: '123456789012', verified_at: '2026-07-14T00:00:00+00:00', note: '' },
                { name: 'my-sandbox', region: 'us-east-1', account: '', verified_at: '', note: '' },
              ],
              default: opts?.default ?? 'my-deploy',
              available: opts?.available ?? ['other-sso'],
            }
          : { profile: 'my-deploy', region: 'us-west-2' }
    return { ok: true, status: 200, json: async () => body } as unknown as Response
  }))
}

function deployedWebapp(slug: string, maxUsd: number) {
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
      origin_session: 's',
      deploy_target: { provider: 'aws', account: '123456789012', region: 'us-west-2', public_url: `https://d.cloudfront.net/${slug}/`, profile: 'my-deploy' },
      architecture: { tier: '3', frontend: 'cf', backend: 'apigw', state: 'ddb', resources: [] },
      lifecycle: { created_at: '', expires_at: null, persistent: true, ttl_hours: 72, status: 'live' },
      cost: { model: 'ttl-window', window_hours: 72, estimates: [{ views: 100, usd: 0.0009 }, { views: 10000, usd: maxUsd }], idle_usd: 0, note: '' },
      teardown: { handle: slug, reversible: false },
    },
  }
}

function renderPage(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={qc}>{ui}</QueryClientProvider></MemoryRouter>)
}

describe('ArtifactDeployPage (Artifact Deploy console)', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('renders the Artifact Deploy header', async () => {
    mockFetch([])
    renderPage(<ArtifactDeployPage />)
    expect(screen.getByText('Artifact Deploy')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText(/Deployments \(1\)/)).toBeInTheDocument())
  })

  it('renders both static and webapp rows in a unified Deployments table', async () => {
    mockFetch([deployedWebapp('kanban', 0.0881)])
    renderPage(<ArtifactDeployPage />)
    // Unified table shows combined count: 1 static + 1 webapp = 2
    await waitFor(() => expect(screen.getByText(/Deployments \(2\)/)).toBeInTheDocument())
    // Static row has type badge (may also appear in policy-tier selector)
    expect(screen.getAllByText('static').length).toBeGreaterThanOrEqual(1)
    // Webapp row has type badge
    expect(screen.getByText('webapp')).toBeInTheDocument()
    // Both names present
    expect(screen.getByText('blog')).toBeInTheDocument()
    expect(screen.getByText('kanban')).toBeInTheDocument()
    // Webapp details link
    expect(screen.getByText('Details').closest('a')).toHaveAttribute('href', '/artifacts/kanban')
  })

  it('shows webapp status, link, and per-app cost in the unified table', async () => {
    mockFetch([deployedWebapp('kanban', 0.0881)])
    renderPage(<ArtifactDeployPage />)
    await waitFor(() => expect(screen.getByText(/Deployments \(2\)/)).toBeInTheDocument())
    expect(screen.getByText('live')).toBeInTheDocument()
    expect(screen.getByText('https://d.cloudfront.net/kanban/')).toBeInTheDocument()
    expect(screen.getAllByText(/\$0\.0881/).length).toBeGreaterThanOrEqual(1)
  })

  it('shows static site actions (Recall + Destroy) in the unified table', async () => {
    mockFetch([])
    renderPage(<ArtifactDeployPage />)
    await waitFor(() => expect(screen.getByText(/Deployments \(1\)/)).toBeInTheDocument())
    expect(screen.getByText('Recall')).toBeInTheDocument()
    expect(screen.getByText('Destroy')).toBeInTheDocument()
  })

  it('aggregates the account total across static sites + webapps under the table', async () => {
    mockFetch([deployedWebapp('a', 0.05), deployedWebapp('b', 0.07)])
    renderPage(<ArtifactDeployPage />)
    await waitFor(() => expect(screen.getByText(/Deployments \(3\)/)).toBeInTheDocument())
    expect(screen.getByText(/Estimated total — 1 static site \+ 2 webapps:/)).toBeInTheDocument()
    expect(screen.getByText('~$0.1200')).toBeInTheDocument()
  })

  it('excludes not-yet-deployed drafts from the unified table and the total', async () => {
    const draft = deployedWebapp('draft-app', 0.5)
    draft.webapp_metadata.deploy_target.public_url = ''
    mockFetch([draft, deployedWebapp('live-app', 0.01)])
    renderPage(<ArtifactDeployPage />)
    // Unified table: 1 static + 1 webapp = 2 (draft excluded)
    await waitFor(() => expect(screen.getByText(/Deployments \(2\)/)).toBeInTheDocument())
    // The draft is NOT a deployment: it lives in Ready-to-deploy (once), not in
    // the table, and its cost stays out of the total.
    expect(screen.getByText(/Ready to deploy \(1\)/)).toBeInTheDocument()
    expect(screen.getAllByText('draft-app')).toHaveLength(1)
    expect(screen.getByText('~$0.0100')).toBeInTheDocument()
  })

  it('renders the AWS Profiles control plane with default star, account, and verify state', async () => {
    mockFetch([])
    renderPage(<ArtifactDeployPage />)
    await waitFor(() => expect(screen.getByText(/AWS Profiles \(2\)/)).toBeInTheDocument())
    expect(screen.getAllByText('my-deploy').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('acct 123456789012')).toBeInTheDocument()
    expect(screen.getByText('verified')).toBeInTheDocument()
    expect(screen.getByText('unverified')).toBeInTheDocument()
    expect(screen.getByLabelText('my-deploy is the default profile')).toBeInTheDocument()
    expect(screen.getByLabelText('Make my-sandbox the default profile')).toBeInTheDocument()
  })

  it('offers one-click registration for profiles discovered in the AWS config', async () => {
    mockFetch([], { available: ['other-sso', 'work-admin'] })
    renderPage(<ArtifactDeployPage />)
    await waitFor(() => expect(screen.getByText(/Found in your AWS config:/)).toBeInTheDocument())
    expect(screen.getByText('other-sso')).toBeInTheDocument()
    expect(screen.getByText('work-admin')).toBeInTheDocument()
  })

  it('shows each deployment row with the profile that owns it', async () => {
    mockFetch([deployedWebapp('kanban', 0.0881)])
    renderPage(<ArtifactDeployPage />)
    await waitFor(() => expect(screen.getByText(/Deployments \(2\)/)).toBeInTheDocument())
    // Static site row chip + webapp row chip + profiles table entry all carry the name.
    expect(screen.getAllByText('my-deploy').length).toBeGreaterThanOrEqual(3)
  })

  it('lets drafts be deployed straight from the console with a chosen profile', async () => {
    const draft = deployedWebapp('parkinglot-draft', 0.05)
    draft.webapp_metadata.deploy_target.public_url = ''
    draft.webapp_metadata.lifecycle.status = 'draft'
    // This test used to assert the opposite: that Deploy set `__mc_chat_launch`
    // and navigated to /chat. That WAS the bug (#12816) — the button on the
    // confirm surface sent a present, cookie-authenticated human to a chat
    // session, and the chat sent them back to the button. Deploy now deploys.
    const calls: Array<Record<string, unknown>> = []
    mockFetch([draft], {
      onDeploy: (body) => {
        calls.push(body)
        return body.confirm === true
          ? { url: 'https://d9.cloudfront.net/', site_id: 'parkinglot-draft' }
          : {
            requires_confirm: true, public: true, site_id: 'parkinglot-draft',
            bytes: 2048, scan: 'clean', profile: 'my-sandbox', region: 'us-east-1',
            content_digest: 'sha256:abc',
          }
      },
    })
    renderPage(<ArtifactDeployPage />)
    await waitFor(() => expect(screen.getByText(/Ready to deploy \(1\)/)).toBeInTheDocument())
    expect(screen.getByText('parkinglot-draft')).toBeInTheDocument()
    // Radix Select: click the combobox trigger (shows current value) to open
    // the dropdown, then click the desired option
    const profileTrigger = screen.getByRole('combobox', { name: 'Deploy profile for parkinglot-draft' })
    expect(profileTrigger).toHaveTextContent('my-deploy')
    fireEvent.click(profileTrigger)
    await waitFor(() => expect(screen.getByRole('option', { name: 'my-sandbox' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: 'my-sandbox' }))
    fireEvent.click(screen.getByLabelText('Deploy parkinglot-draft'))

    // The preview goes out with the chosen profile, and the TTL defaults to 0 —
    // the only value that needs no auto-cleanup infrastructure, so a first
    // deploy on a fresh account cannot fail its precondition.
    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0].artifact_slug).toBe('parkinglot-draft')
    expect(calls[0].profile).toBe('my-sandbox')
    expect(calls[0].ttl_hours).toBe(0)
    expect(calls[0].confirm).toBeUndefined()

    // The blocking public-by-link acknowledgment stands in front of the deploy.
    fireEvent.click(await screen.findByRole('button', { name: /publish/i }))
    await waitFor(() => expect(calls.length).toBe(2))
    expect(calls[1].confirm).toBe(true)
    expect(calls[1].expected_content_digest).toBe('sha256:abc')
    expect(calls[1].expected_profile).toBe('my-sandbox')

    // And nothing was handed to a chat session.
    expect((window as unknown as { __mc_chat_launch?: unknown }).__mc_chat_launch).toBeFalsy()
  })

  it('offers a Back-to-Artifacts navigation so the console is never a dead end', async () => {
    renderPage(<ArtifactDeployPage />)
    const back = await screen.findByRole('button', { name: /Back to Artifacts/i })
    expect(back).toBeInTheDocument()
  })

  it('labels the cost stat as an estimate, never as a monthly bill', async () => {
    renderPage(<ArtifactDeployPage />)
    expect(await screen.findByText('Est. Cost (not a bill)')).toBeInTheDocument()
    expect(screen.queryByText('Est. Monthly')).toBeNull()
  })

  it('hides the Ready-to-deploy section when there are no drafts', async () => {
    mockFetch([deployedWebapp('kanban', 0.01)])
    renderPage(<ArtifactDeployPage />)
    await waitFor(() => expect(screen.getByText(/Deployments \(2\)/)).toBeInTheDocument())
    expect(screen.queryByText(/Ready to deploy/)).toBeNull()
  })
})
