import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { ReactElement } from 'react'
import WebAppArtifactCard from '../components/WebAppArtifactCard'
import { api } from '../api/client'
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

  // FU-6 (pod field-test): an expired/torn-down card must offer a way back to
  // deployment and must not render a live-looking dead link.
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
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = ''
    artifact.webapp_metadata!.lifecycle.status = 'draft'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    expect(screen.getByText('Not deployed')).toBeInTheDocument()
    const deployBtn = screen.getByRole('button', { name: /Deploy/i })
    expect(deployBtn).toBeInTheDocument()
    // No teardown button in the not-deployed state.
    expect(screen.queryByRole('button', { name: /Tear down/i })).toBeNull()

    // Clicking Deploy sets the chat-launch intent (new session + auto-send).
    fireEvent.click(deployBtn)
    const launch = (window as unknown as { __mc_chat_launch?: { message: string } }).__mc_chat_launch
    expect(launch).toBeTruthy()
    expect(launch!.message).toMatch(/deploy/i)
    expect(launch!.message).toContain('kanban-demo')
  })

  it('offers a deploy-time profile dropdown and bakes the choice into the seed prompt', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        profiles: [{ name: 'my-deploy' }, { name: 'my-sandbox' }],
        default: 'my-deploy',
      }),
    }) as unknown as Response))
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = ''
    artifact.webapp_metadata!.lifecycle.status = 'draft'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    const select = await screen.findByLabelText('AWS profile to deploy with')
    // Default preselected; the other registered profile is offered.
    expect(select).toHaveDisplayValue('profile: my-deploy (default)')
    fireEvent.change(select, { target: { value: 'my-sandbox' } })
    fireEvent.click(screen.getByRole('button', { name: /^Deploy$/i }))
    const launch = (window as unknown as { __mc_chat_launch?: { message: string } }).__mc_chat_launch
    expect(launch!.message).toContain('Use the AWS profile "my-sandbox".')
    vi.unstubAllGlobals()
  })

  it('falls back to the default profile in the seed when nothing is picked', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ profiles: [{ name: 'my-deploy' }], default: 'my-deploy' }),
    }) as unknown as Response))
    const artifact = makeArtifact()
    artifact.webapp_metadata!.deploy_target.public_url = ''
    artifact.webapp_metadata!.lifecycle.status = 'draft'

    renderWithClient(<WebAppArtifactCard artifact={artifact} />)

    await screen.findByLabelText('AWS profile to deploy with')
    fireEvent.click(screen.getByRole('button', { name: /^Deploy$/i }))
    const launch = (window as unknown as { __mc_chat_launch?: { message: string } }).__mc_chat_launch
    expect(launch!.message).toContain('Use the AWS profile "my-deploy".')
    vi.unstubAllGlobals()
  })
})
