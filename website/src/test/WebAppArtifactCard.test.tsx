import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactElement } from 'react'
import WebAppArtifactCard from '../components/WebAppArtifactCard'
import { api } from '../api/client'
import { framablePreviewUrl } from '../lib/safeUrl'
import { PREVIEW_ARTIFACT_DEPLOY } from '../utils/previewFlags'
import type { Artifact } from '../types'

vi.mock('../api/client', () => ({
  api: {
    artifactTeardown: vi.fn(),
  },
}))

function makeArtifact(overrides?: Partial<Artifact>): Artifact {
  return {
    slug: 'kanban-demo',
    name: 'Kanban Demo',
    kind: 'webapp',
    source: 'chat',
    description: 'A kanban board deployed to AWS',
    tags: ['deploy'],
    version: 1,
    created_at: '2026-07-10T10:00:00Z',
    updated_at: '2026-07-10T10:00:00Z',
    webapp_metadata: {
      slug: 'kanban-demo',
      origin_session: 'abc123',
      deploy_target: {
        provider: 'aws',
        account: '123456789012',
        region: 'us-west-2',
        public_url: 'https://d2nzmpzyp0popu.cloudfront.net/kanban/',
        profile: 'my-deploy',
      },
      architecture: {
        tier: '3',
        frontend: 'CloudFront -> S3 (private, OAC)',
        backend: 'API Gateway HTTP API -> Lambda',
        state: 'DynamoDB (IAM scoped to table)',
        resources: [
          { type: 'frontend', id: 's3://kirocrew-deploy-base/kanban/' },
          { type: 'backend', id: 'kirocrew-deploy-app-kanban-demo' },
          { type: 'state', id: 'kirocrew-deploy-app-kanban-demo-table' },
        ],
      },
      lifecycle: {
        created_at: new Date(Date.now() - 24 * 3600e3).toISOString(),
        // Dynamic: always ~48h in the future so the fixture never ages into
        // "expired" (a hardcoded date here time-bombed the suite once).
        expires_at: new Date(Date.now() + 48 * 3600e3).toISOString(),
        persistent: false,
        ttl_hours: 72,
        status: 'live',
      },
      cost: {
        model: 'ttl-window',
        window_hours: 72,
        estimates: [
          { views: 100, usd: 0.0009 },
          { views: 1000, usd: 0.009 },
          { views: 10000, usd: 0.088 },
        ],
        idle_usd: 0,
        note: 'estimate, not the AWS bill',
      },
      teardown: {
        method: 'reaper-lambda',
        handle: 'kanban-demo',
        reversible: false,
      },
    },
    ...overrides,
  }
}

function renderWithClient(ui: ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('WebAppArtifactCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders public URL, architecture rows, and cost tiles for a stateful app', () => {
    renderWithClient(<WebAppArtifactCard artifact={makeArtifact()} />)

    // Public URL
    expect(screen.getByText('https://d2nzmpzyp0popu.cloudfront.net/kanban/')).toBeInTheDocument()

    // Architecture rows
    expect(screen.getByText(/CloudFront -> S3/)).toBeInTheDocument()
    expect(screen.getByText(/API Gateway HTTP API -> Lambda/)).toBeInTheDocument()
    expect(screen.getByText(/DynamoDB/)).toBeInTheDocument()

    // Cost tiles
    expect(screen.getByText('100 views')).toBeInTheDocument()
    expect(screen.getByText('1,000 views')).toBeInTheDocument()
    expect(screen.getByText('10,000 views')).toBeInTheDocument()
    expect(screen.getByText('$0.0009')).toBeInTheDocument()

    // Status badge
    expect(screen.getByText('live')).toBeInTheDocument()

    // Profile chip (display-only: which AWS profile the deploy ran with)
    expect(screen.getByText(/profile my-deploy/)).toBeInTheDocument()
  })

  it('shows persistent indicator when persistent is true', () => {
    const artifact = makeArtifact()
    artifact.webapp_metadata!.lifecycle.persistent = true
    artifact.webapp_metadata!.lifecycle.expires_at = null

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    expect(screen.getByText('persistent')).toBeInTheDocument()
    expect(screen.getByLabelText('persistent')).toBeInTheDocument()
  })

  it('calls teardown API on confirm and reflects expired state', async () => {
    const onTornDown = vi.fn()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    ;(api.artifactTeardown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true })

    renderWithClient(<WebAppArtifactCard artifact={makeArtifact()} onTornDown={onTornDown} />)

    const btn = screen.getByRole('button', { name: /Cancel \/ Tear down/i })
    fireEvent.click(btn)

    await waitFor(() => {
      expect(api.artifactTeardown).toHaveBeenCalledWith('kanban-demo')
    })

    await waitFor(() => {
      expect(onTornDown).toHaveBeenCalled()
    })

    // Badge should show expired
    await waitFor(() => {
      expect(screen.getByText('Expired')).toBeInTheDocument()
    })
  })

  // An expired/torn-down card must offer a way back to deployment and must not
  // render a live-looking dead link.
  it('expired state shows Redeploy and hides the stale public link', () => {
    const artifact = makeArtifact()
    artifact.webapp_metadata!.lifecycle.status = 'expired'
    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    // Redeploy affordance exists
    expect(screen.getByRole('button', { name: /Redeploy/i })).toBeInTheDocument()
    // The dead URL is not rendered as a link; a torn-down note is shown instead
    expect(screen.queryByRole('link', { name: /cloudfront/i })).not.toBeInTheDocument()
    expect(screen.getByText(/torn down/i)).toBeInTheDocument()
    // Tear down stays disabled in the tombstone state
    expect(screen.getByRole('button', { name: /Cancel \/ Tear down/i })).toBeDisabled()
  })

  it('live state still renders the public link and no Redeploy', () => {
    renderWithClient(<WebAppArtifactCard artifact={makeArtifact()} />)
    expect(
      screen.getByRole('link', { name: 'https://d2nzmpzyp0popu.cloudfront.net/kanban/' }),
    ).toHaveAttribute('href', 'https://d2nzmpzyp0popu.cloudfront.net/kanban/')
    expect(screen.queryByRole('button', { name: /Redeploy/i })).not.toBeInTheDocument()
  })

  it('does not render a javascript: public_url as a clickable link', () => {
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = 'javascript:alert(1)'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    // The value is still shown (as inert text) but never as an anchor href.
    const shown = screen.getByText('javascript:alert(1)')
    expect(shown.tagName).not.toBe('A')
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull()
  })

  it('renders a neutral label (never NaN) for an unparseable expires_at', () => {
    const artifact = makeArtifact()
    artifact.webapp_metadata!.lifecycle.persistent = false
    artifact.webapp_metadata!.lifecycle.expires_at = 'not-a-date'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    expect(screen.getByText('no expiry set')).toBeInTheDocument()
    expect(screen.queryByText(/NaN/)).toBeNull()
  })

  it('labels a not-yet-stamped app (no expiry, not persistent) as "no expiry set"', () => {
    const artifact = makeArtifact()
    artifact.webapp_metadata!.lifecycle.persistent = false
    artifact.webapp_metadata!.lifecycle.expires_at = null

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    // A "deploying" app whose TTL is not stamped yet must NOT read "∞ persistent".
    expect(screen.getByText('no expiry set')).toBeInTheDocument()
    expect(screen.queryByLabelText('persistent')).toBeNull()
  })

  it('shows a Deploy button (not the control card) for an app not deployed yet', () => {
    // The Deploy hero sits behind the Artifact Deploy Feature Preview, so a test
    // about the deploy affordance opts in. The default-off contract is its own
    // case below.
    localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1')
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = ''
    artifact.webapp_metadata!.lifecycle.status = 'draft'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    expect(screen.getByText('Not deployed')).toBeInTheDocument()
    const deployBtn = screen.getByRole('button', { name: /Deploy/i })
    expect(deployBtn).toBeInTheDocument()
    // No teardown button in the not-deployed state.
    expect(screen.queryByRole('button', { name: /Tear down/i })).toBeNull()

    // Clicking Deploy DEPLOYS. It used to set a chat-launch intent and navigate
    // away, which is the loop #12816 was reported for: the button on the card
    // sent a present, authenticated human to a chat that sent them back.
    fireEvent.click(deployBtn)
    expect((window as unknown as { __mc_chat_launch?: unknown }).__mc_chat_launch).toBeFalsy()
  })

  it('hides every deploy affordance AND the invitation until the preview is on', () => {
    // Default OFF hides the feature from ordinary users entirely: no route, and
    // no copy naming an action they cannot take. The card still describes the
    // app -- withholding information ABOUT the artifact would gate the wrong
    // thing -- and points at where deployment lives.
    localStorage.removeItem(PREVIEW_ARTIFACT_DEPLOY)
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = ''
    artifact.webapp_metadata!.lifecycle.status = 'draft'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    // The app's own draft information stays.
    expect(screen.getByText('Not deployed')).toBeInTheDocument()
    // No route to a deploy, by either path.
    expect(screen.queryByRole('button', { name: /^Deploy$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Deploy via agent' })).toBeNull()
    // And no invitation to take an action that has no control.
    expect(screen.queryByText(/deploy to your own AWS account/i)).toBeNull()
    expect(
      screen.getByText(/Deployment is available under Settings → Developer → Feature Previews\./i),
    ).toBeInTheDocument()
  })

  /** Profiles endpoint plus a deploy endpoint that records what it was sent. */
  const stubDeployFetch = (profiles: string[], dflt: string) => {
    // Every caller of this helper is exercising the deploy path, which the
    // Artifact Deploy Feature Preview gates, so opting in belongs here rather
    // than repeated in each case.
    localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1')
    const sent: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url)
      const ok = (data: unknown) => ({
        ok: true, status: 200, json: async () => data,
      }) as unknown as Response
      if (u.endsWith('/deploy/deploy')) {
        sent.push(init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {})
        return ok({
          requires_confirm: true, public: true, site_id: 'kanban-demo',
          bytes: 10, scan: 'clean', profile: 'x', region: 'us-west-2', content_digest: 'sha256:z',
        })
      }
      return ok({ profiles: profiles.map((name) => ({ name })), default: dflt })
    }))
    return sent
  }

  it('offers a deploy-time profile dropdown and deploys with the chosen profile', async () => {
    const sent = stubDeployFetch(['my-deploy', 'my-sandbox'], 'my-deploy')
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = ''
    artifact.webapp_metadata!.lifecycle.status = 'draft'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    const select = await screen.findByLabelText('AWS profile to deploy with')
    // Default preselected; the other registered profile is offered.
    // `SimpleSelect` wraps a Radix Select, so a `change` event on the trigger
    // does nothing — open it, then click the option.
    expect(select).toHaveTextContent('profile: my-deploy (default)')
    fireEvent.click(select)
    fireEvent.click(await screen.findByRole('option', { name: 'profile: my-sandbox' }))
    fireEvent.click(screen.getByRole('button', { name: /^Deploy$/i }))
    // The choice reaches the deploy request itself, not a sentence in a prompt.
    await waitFor(() => expect(sent.length).toBe(1))
    expect(sent[0].profile).toBe('my-sandbox')
    expect(sent[0].artifact_slug).toBe('kanban-demo')
    vi.unstubAllGlobals()
  })

  it('falls back to the default profile when nothing is picked', async () => {
    const sent = stubDeployFetch(['my-deploy'], 'my-deploy')
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = ''
    artifact.webapp_metadata!.lifecycle.status = 'draft'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    await screen.findByLabelText('AWS profile to deploy with')
    fireEvent.click(screen.getByRole('button', { name: /^Deploy$/i }))
    await waitFor(() => expect(sent.length).toBe(1))
    expect(sent[0].profile).toBe('my-deploy')
    vi.unstubAllGlobals()
  })

  // ------------------------------------------------------------- redesign

  const stubPreviewFetch = (remoteFramable: boolean) =>
    vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () =>
        String(url).includes('/app-preview')
          ? { available: false, remote_framable: remoteFramable }
          : { profiles: [], default: '' },
    }) as unknown as Response))

  it('live CloudFront deployment embeds a sandboxed site preview iframe', async () => {
    // The remote iframe renders only after the gateway probe confirms the
    // deployed site is framable.
    stubPreviewFetch(true)
    renderWithClient(<WebAppArtifactCard artifact={makeArtifact()} />)
    const frame = (await screen.findByTitle('Live preview: kanban-demo')) as HTMLIFrameElement
    expect(frame.tagName).toBe('IFRAME')
    expect(frame.src).toBe('https://d2nzmpzyp0popu.cloudfront.net/kanban/')
    // Sandboxed, no top-navigation escape.
    expect(frame.getAttribute('sandbox')).not.toContain('allow-top-navigation')
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer')
    vi.unstubAllGlobals()
  })

  it('legacy stack (probe says not framable) shows the hero fallback, never a blank iframe (R7)', async () => {
    stubPreviewFetch(false)
    renderWithClient(<WebAppArtifactCard artifact={makeArtifact()} />)
    expect(await screen.findByText(/Preview unavailable for this host/)).toBeInTheDocument()
    expect(screen.queryByTitle('Live preview: kanban-demo')).toBeNull()
    vi.unstubAllGlobals()
  })

  it('does not embed a preview iframe for a non-CloudFront public_url', () => {
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = 'https://evil-cloudfront.net.attacker.example/app/'
    renderWithClient(<WebAppArtifactCard artifact={artifact} />)
    expect(screen.queryByTitle('Live preview: kanban-demo')).toBeNull()
    expect(screen.getByText(/Preview unavailable for this host/)).toBeInTheDocument()
  })

  it('expired card renders no countdown even when a legacy tombstone kept a future expires_at (FU-7)', () => {
    const artifact = makeArtifact()
    // Legacy tombstone: status expired but expires_at survived.
    artifact.webapp_metadata!.lifecycle.status = 'expired'
    renderWithClient(<WebAppArtifactCard artifact={artifact} />)
    expect(screen.getByText('Expired')).toBeInTheDocument()
    expect(screen.queryByText(/expires in/)).toBeNull()
    expect(screen.queryByText(/Time to live/)).toBeNull()
    // And the dead deployment must not embed a live preview.
    expect(screen.queryByTitle('Live preview: kanban-demo')).toBeNull()
  })

  it('marks cost as a what-if estimate, never a bill (Joe R2)', () => {
    renderWithClient(<WebAppArtifactCard artifact={makeArtifact()} />)
    expect(screen.getByText('estimate')).toBeInTheDocument()
    expect(screen.getByText(/What-if traffic scenarios/)).toBeInTheDocument()
    expect(screen.getByText(/you pay only for actual usage/)).toBeInTheDocument()
  })

  it('deploying state shows the deploying hero, not an iframe', () => {
    const artifact = makeArtifact()
    artifact.webapp_metadata!.lifecycle.status = 'deploying'
    renderWithClient(<WebAppArtifactCard artifact={artifact} />)
    expect(screen.getByText(/Deploying/)).toBeInTheDocument()
    expect(screen.queryByTitle('Live preview: kanban-demo')).toBeNull()
  })
})

describe('framablePreviewUrl', () => {
  it('accepts a first-party CloudFront distribution URL', () => {
    expect(framablePreviewUrl('https://d2nzmpzyp0popu.cloudfront.net/kanban/')).toBe(
      'https://d2nzmpzyp0popu.cloudfront.net/kanban/',
    )
  })

  it.each([
    ['http (not https)', 'http://d2nzmpzyp0popu.cloudfront.net/kanban/'],
    ['userinfo smuggling', 'https://user:pass@d2nzmpzyp0popu.cloudfront.net/'],
    ['lookalike suffix host', 'https://evil-cloudfront.net/app/'],
    ['cloudfront.net as prefix of attacker domain', 'https://d123.cloudfront.net.evil.example/'],
    ['subdomain depth mismatch', 'https://a.b.cloudfront.net/'],
    ['bare cloudfront.net', 'https://cloudfront.net/'],
    ['custom domain', 'https://app.example.com/'],
    ['javascript scheme', 'javascript:alert(1)'],
    ['not a URL', 'not a url'],
    ['empty', ''],
  ])('rejects %s', (_label, url) => {
    expect(framablePreviewUrl(url)).toBeNull()
  })
})
