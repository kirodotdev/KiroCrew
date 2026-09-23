//
// The Ready-to-deploy row reported two states at once. The status cell read the
// artifact record, whose `status: 'draft'` only becomes `deployed` on a later
// refetch, so a finished deploy rendered "Deployed!" directly beside a
// "not deployed" badge and an enabled Deploy button. A reader could not tell
// which of the two to believe.
//
// The primary button had the same shape of problem in the scan-blocked state: it
// stayed bright above text saying the findings cannot be overridden, so pressing
// it again looked like it might do something.
//
// What these pin: after a deploy the row agrees with itself, and the primary is
// inert in both states where pressing it achieves nothing.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ArtifactDeployPage from '../pages/ArtifactDeployPage'

function draft(slug: string) {
  return {
    slug, name: slug, kind: 'webapp', source: 'chat', description: '', tags: [],
    version: 1, created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
    webapp_metadata: {
      slug, origin_session: 's', app_dir: '/w/' + slug,
      deploy_target: { provider: 'aws', account: '1', region: 'us-west-2', public_url: '', profile: '' },
      architecture: { tier: 'static', frontend: 'cf', backend: '', state: '', resources: [] },
      lifecycle: { created_at: '', expires_at: null, persistent: false, ttl_hours: 0, status: 'draft' },
      cost: { model: 'ttl-window', window_hours: 0, estimates: [], idle_usd: 0, note: '' },
      teardown: { handle: slug, reversible: false },
    },
  }
}

const SCAN_409 = {
  blocked: true,
  reason: 'scan',
  findings: 'aws-access-key-id in config/settings.json:14',
  count: 1,
  credential: true,
}

/** `outcome` selects what the CONFIRM call answers; the preview always succeeds. */
function installFetch(outcome: 'done' | 'scan' | 'bare500') {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url)
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {}
    const ok = (data: unknown, status = 200) => ({
      ok: status < 400, status, json: async () => data,
      text: async () => JSON.stringify(data), headers: { get: () => null },
    }) as unknown as Response
    if (u.endsWith('/deploy/deploy')) {
      if (body.confirm !== true) {
        return ok({
          requires_confirm: true, public: true, site_id: 'shopfront',
          bytes: 100, scan: 'clean', profile: 'ship', region: 'us-west-2',
          content_digest: 'sha256:d',
        })
      }
      if (outcome === 'done') return ok({ url: 'https://d1.cloudfront.net/', site_id: 'shopfront' })
      if (outcome === 'bare500') {
        // A proxy or disk-full 500 with a non-JSON body: the hook's own parse
        // turns it into {}, so there is no `error` key to read.
        return {
          ok: false, status: 500, json: async () => { throw new Error('not json') },
          text: async () => '<html>502 Bad Gateway</html>', headers: { get: () => null },
        } as unknown as Response
      }
      return ok(SCAN_409, 409)
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
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><ArtifactDeployPage /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Deploy the one draft row through the acknowledgment. */
async function deployTheRow() {
  await screen.findByText(/Ready to deploy \(1\)/)
  fireEvent.click(screen.getByLabelText('Deploy shopfront'))
  fireEvent.click(await screen.findByRole('button', { name: /publish/i }))
}

describe('Ready-to-deploy row — one state per row', () => {
  beforeEach(() => { vi.restoreAllMocks() })
  afterEach(() => { vi.unstubAllGlobals() })

  it('badges the row deployed once its own deploy finishes', async () => {
    installFetch('done')
    renderPage()
    expect(await screen.findByText('not deployed')).toBeTruthy()
    await deployTheRow()
    // Two nodes carry it on purpose: the status badge and the flow's own success
    // line. Agreeing is the point — the defect was a badge that disagreed.
    await waitFor(() => expect(screen.getAllByText('Deployed!').length).toBe(2))
    // The record still says draft, so a row reading the record would keep the
    // warn badge beside its own success line.
    await waitFor(() => expect(screen.queryByText('not deployed')).toBeNull())
  })

  it('makes the primary inert once the deploy is done', async () => {
    installFetch('done')
    renderPage()
    await deployTheRow()
    await waitFor(() => expect(screen.getAllByText('Deployed!').length).toBeGreaterThan(0))
    await waitFor(() =>
      expect(screen.getByLabelText('Deploy shopfront').hasAttribute('disabled')).toBe(true))
  })

  it('makes the primary inert while a scan block is showing', async () => {
    installFetch('scan')
    renderPage()
    await deployTheRow()
    await screen.findByText(/config\/settings\.json:14/)
    await waitFor(() =>
      expect(screen.getByLabelText('Deploy shopfront').hasAttribute('disabled')).toBe(true))
  })

  it('never reports a non-2xx with an unreadable body as a deploy', async () => {
    // The hook parses a non-JSON body into {}, so an infra 500 behind a proxy
    // carries neither `error` nor `code`. Treating "no error key" as success
    // reported a deploy that never happened AND wrote the deployed state back
    // onto the artifact, which is the worst of the two failure modes.
    installFetch('bare500')
    renderPage()
    await deployTheRow()
    await waitFor(() => expect(screen.queryAllByText('Deployed!').length).toBe(0))
    expect(screen.getByText('not deployed')).toBeTruthy()
  })
})
