// The auto-cleanup precondition, as a human meets it (issue #12816, ruling F).
//
// The default TTL used to be 72 hours and nothing in the page's setup guide
// installed the reaper, so a first confirm reliably hit a 409 whose text was
// "Finite-TTL deploys require the reaper base stack (kirocrew-deploy-base). Use
// ttl_hours=0 for persistent or install the reaper (install-reaper.sh)." — four
// product nouns in a red banner and no way forward.
//
// What these pin: the banner is a plain sentence, the jargon is one click away,
// and the two ways out actually work.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ArtifactDeployPage from '../pages/ArtifactDeployPage'

const REAPER_409 = {
  error: 'This deploy is set to expire in 72 hours, but your AWS account has no '
    + 'auto-cleanup installed yet. Deploy it as permanent instead, or install '
    + 'auto-cleanup first.',
  code: 'reaper_required',
  details: 'Finite-TTL deploys require the reaper base stack (kirocrew-deploy-base). '
    + 'Use ttl_hours=0 for persistent or install the reaper (install-reaper.sh).',
  remediation: '/home/u/.kirocrew/skills/artifact-deploy/scripts/install-reaper.sh '
    + '--profile ship --region us-west-2',
}

function draft(slug: string) {
  return {
    slug, name: slug, kind: 'webapp', source: 'chat', description: '', tags: [],
    version: 1, created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
    webapp_metadata: {
      slug, origin_session: 's', app_dir: '/w/' + slug,
      deploy_target: { provider: 'aws', account: '1', region: 'us-west-2', public_url: '', profile: '' },
      architecture: { tier: 'static', frontend: 'cf', backend: '', state: '', resources: [] },
      lifecycle: { created_at: '', expires_at: null, persistent: false, ttl_hours: 72, status: 'draft' },
      cost: { model: 'ttl-window', window_hours: 72, estimates: [], idle_usd: 0, note: '' },
      teardown: { handle: slug, reversible: false },
    },
  }
}

/** Scripts the deploy endpoint: preview succeeds, the first confirm hits the
 *  precondition, and a confirm carrying ttl_hours=0 succeeds. */
function installFetch() {
  const bodies: Array<Record<string, unknown>> = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {}
    const ok = (data: unknown, status = 200) => ({
      ok: status < 400, status, json: async () => data,
      text: async () => JSON.stringify(data), headers: { get: () => null },
    }) as unknown as Response
    if (u.endsWith('/deploy/deploy')) {
      bodies.push(body)
      if (body.confirm !== true) {
        return ok({
          requires_confirm: true, public: true, site_id: 'shopfront',
          bytes: 100, scan: 'clean', profile: 'ship', region: 'us-west-2',
          content_digest: 'sha256:d',
        })
      }
      if (body.ttl_hours === 0) return ok({ url: 'https://d1.cloudfront.net/', site_id: 'shopfront' })
      return ok(REAPER_409, 409)
    }
    if (u.startsWith('/api/artifacts')) return ok({ artifacts: [draft('shopfront')] })
    if (u.endsWith('/list')) return ok({ sites: [], configured: true })
    if (u.endsWith('/pending')) return ok({ pending: [] })
    if (u.includes('/profiles')) {
      return ok({
        profiles: [{ name: 'ship', region: 'us-west-2', account: '1', verified_at: '', note: '' }],
        default: 'ship', available: [],
      })
    }
    return ok({ profile: 'ship', region: 'us-west-2', reaperInstallScript: '/s/install-reaper.sh' })
  }))
  return bodies
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><ArtifactDeployPage /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Walk the flow to the point where the precondition has been reported. */
async function reachPrecondition(bodies: Array<Record<string, unknown>>) {
  await screen.findByText(/Ready to deploy \(1\)/)
  // The row defaults to permanent, which needs no auto-cleanup at all — so the
  // precondition is only reachable by explicitly choosing an expiring window.
  fireEvent.click(screen.getByRole('combobox', { name: /TTL/i }))
  fireEvent.click(await screen.findByRole('option', { name: /72 hours/ }))
  fireEvent.click(screen.getByLabelText('Deploy shopfront'))
  fireEvent.click(await screen.findByRole('button', { name: /publish/i }))
  await waitFor(() => expect(bodies.length).toBe(2))
}

describe('Artifact Deploy — the auto-cleanup precondition', () => {
  beforeEach(() => {
    ;(window as unknown as { __mc_chat_launch?: unknown }).__mc_chat_launch = undefined
  })
  afterEach(() => vi.unstubAllGlobals())

  it('states the problem in plain words and keeps the stack names out of the banner', async () => {
    const bodies = installFetch()
    renderPage()
    await reachPrecondition(bodies)

    expect(await screen.findByText(/no auto-cleanup installed yet/)).toBeInTheDocument()
    // The banner names no stack and no parameter. That was the complaint.
    expect(screen.queryByText(/kirocrew-deploy-base/)).toBeNull()
    expect(screen.queryByText(/ttl_hours/)).toBeNull()
  })

  it('puts the stack names and the exact install command behind Details', async () => {
    const bodies = installFetch()
    renderPage()
    await reachPrecondition(bodies)
    await screen.findByText(/no auto-cleanup installed yet/)

    const cmd = /install-reaper\.sh --profile ship --region us-west-2/
    // Step 4 of the setup guide shows an install command too, so counting is
    // what proves the TOGGLE revealed one rather than the guide already having.
    const before = screen.queryAllByText(cmd).length
    fireEvent.click(screen.getByRole('button', { name: 'Details' }))
    expect(await screen.findByText(/kirocrew-deploy-base/)).toBeInTheDocument()
    // The command is absolute — the bare script name is on nobody's PATH.
    expect(screen.queryAllByText(cmd).length).toBe(before + 1)
  })

  it('deploys as permanent without making the user start over', async () => {
    const bodies = installFetch()
    renderPage()
    await reachPrecondition(bodies)
    await screen.findByText(/no auto-cleanup installed yet/)

    fireEvent.click(screen.getByRole('button', { name: /Deploy as permanent/ }))
    // Switching the exposure window is a different decision, so it goes through
    // the acknowledgment again rather than confirming under the earlier consent.
    fireEvent.click(await screen.findByRole('button', { name: /publish/i }))
    await waitFor(() => expect(bodies.length).toBe(3))
    // Same artifact, same previewed content, only the expiry changed to the one
    // value that needs no infrastructure.
    expect(bodies[2].confirm).toBe(true)
    expect(bodies[2].ttl_hours).toBe(0)
    expect(bodies[2].artifact_slug).toBe('shopfront')
    // The previewed bindings survive the retry, so content that changed since the
    // preview cannot be published under the earlier acknowledgment.
    expect(bodies[2].expected_content_digest).toBe('sha256:d')
    expect(await screen.findByText(/d1\.cloudfront\.net/)).toBeInTheDocument()
  })

  it('never hands the user to a chat session for this', async () => {
    const bodies = installFetch()
    renderPage()
    await reachPrecondition(bodies)
    await screen.findByText(/no auto-cleanup installed yet/)
    expect((window as unknown as { __mc_chat_launch?: unknown }).__mc_chat_launch).toBeFalsy()
  })
})
