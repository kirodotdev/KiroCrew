/**
 * AutoImprovementPage: the ruler card, the pull-request table and the findings ledger.
 *
 * The page reads three endpoints and one per-PR status route. These tests answer that route and the
 * ledger from a fake fetch, and they assert what the operator gets: the ruler label and band, the PR
 * verdict, checks and reason, the "Unavailable" badge for a PR the provider could not read, the commit
 * link for an already-committed finding, and the confirm-before-publish gate on a commit. The agent
 * session hook is mocked so that the discuss buttons can be checked against the session they open; the
 * real `truncate` stays in place.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

const openSession = vi.hoisted(() => vi.fn())

vi.mock('./lib/agentSession', async (importOriginal) => {
  const mod = await importOriginal<typeof import('./lib/agentSession')>()
  return { ...mod, useAgentSession: () => ({ openSession, busy: false }) }
})

import { createTestStore } from '../../test/helpers'
import AutoImprovementPage from './AutoImprovementPage'

const FINDINGS = [
  { fp: 'f1', kind: 'bug', target: 'src/cache.py::get', status: 'filed', pr: 'https://github.com/zzq/fabric/pull/9' },
  { fp: 'f2', kind: 'perf', target: 'src/loop.py::run', status: 'committed', cr: 'abc1234' },
  { fp: 'f3', kind: 'lint', target: 'src/x.py::y', status: 'no_defect' },
  { fp: 'f4', kind: 'lint', target: 'src/z.py::w', status: 'mystery' },
  { fp: 'f5', kind: 'bug', target: 'src/gone.py::v', status: 'filed', pr: 'https://github.com/zzq/fabric/pull/10' },
]

const PR_STATUS: Record<string, unknown> = {
  '9': {
    ok: true, url: 'https://github.com/zzq/fabric/pull/9', number: 9, title: 'Tune the cache',
    verdict: 'READY', verdictReason: 'all gates green', checks: { label: '12/12 green' },
  },
  '10': { ok: false, url: 'https://github.com/zzq/fabric/pull/10', error: 'provider down' },
}

// Chain: GET /findings, then one GET /pr-status per PR row. Two sequential answers can exceed the
// 1000 ms default, so waits on PR status allow 5000 ms.
const PR_CHAIN = { timeout: 5000 }

let ruler: Record<string, unknown> = {}
let fetchMock: ReturnType<typeof vi.fn>

function respond(url: string, init?: RequestInit) {
  if (init?.method === 'POST') return { ok: true, status: 200, json: async () => ({ ok: true }) }
  if (url.includes('/pr-status')) {
    const prUrl = new URL(url, 'http://local').searchParams.get('url') ?? ''
    const number = prUrl.split('/').pop() ?? ''
    return { ok: true, status: 200, json: async () => PR_STATUS[number] }
  }
  if (url.endsWith('/findings/f3')) {
    return { ok: true, status: 200, json: async () => ({ finding: { candidate: { signature: 'nil deref in cache get' } } }) }
  }
  if (url.includes('/findings')) return { ok: true, status: 200, json: async () => ({ findings: FINDINGS }) }
  if (url.includes('/ruler')) return { ok: true, status: 200, json: async () => ruler }
  if (url.includes('/config')) {
    return { ok: true, status: 200, json: async () => ({ target_display: 'zzq/fabric', branch: 'origin/main' }) }
  }
  return { ok: true, status: 200, json: async () => ({}) }
}

function mountPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={createTestStore()}>
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <AutoImprovementPage />
        </QueryClientProvider>
      </MemoryRouter>
    </Provider>,
  )
}

beforeEach(() => {
  openSession.mockReset()
  ruler = { status: 'calibrated', primary: { label: 'p95 latency' }, noiseBand: { value: 3, unit: '%' } }
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => respond(String(input), init))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('AutoImprovementPage ruler card', () => {
  it('shows the primary metric and the noise band once the ruler is calibrated', async () => {
    mountPage()
    expect(await screen.findByText(/p95 latency/)).toBeTruthy()
    expect(screen.getByText(/3%/)).toBeTruthy()
  })

  it('says the ruler is not calibrated yet and no cycle will run', async () => {
    ruler = { status: 'uncalibrated' }
    mountPage()
    expect(await screen.findByText(/not calibrated yet/)).toBeTruthy()
  })

  it('opens a ruler session carrying the repository and the ruler state', async () => {
    mountPage()
    await screen.findByText('zzq/fabric')
    await screen.findByText(/p95 latency/)
    fireEvent.click(screen.getByRole('button', { name: 'Discuss the ruler' }))
    expect(openSession).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'ruler',
        id: 'current',
        repo: 'zzq/fabric',
        prompt: expect.stringContaining('p95 latency'),
      }),
    )
  })
})

describe('AutoImprovementPage pull-request table', () => {
  it('renders the PR number, verdict, checks and reason from the status route', async () => {
    mountPage()
    const link = await screen.findByRole('link', { name: /#9/ }, PR_CHAIN)
    expect(link.getAttribute('href')).toBe('https://github.com/zzq/fabric/pull/9')
    expect(await screen.findByText('READY', {}, PR_CHAIN)).toBeTruthy()
    expect(screen.getByText('12/12 green')).toBeTruthy()
    expect(screen.getByText('all gates green')).toBeTruthy()
    const statusCalls = fetchMock.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/pr-status'))
    expect(statusCalls.some((u) => u.includes(encodeURIComponent('https://github.com/zzq/fabric/pull/9')))).toBe(true)
  })

  it('marks a PR the provider could not read as Unavailable instead of a verdict', async () => {
    mountPage()
    await screen.findByText('READY', {}, PR_CHAIN)
    expect(await screen.findByText('Unavailable', {}, PR_CHAIN)).toBeTruthy()
  })

  it('re-reads the PR status when its refresh control is pressed', async () => {
    mountPage()
    const refresh = (await screen.findAllByRole('button', { name: 'Refresh pull request status' }))[0]
    await screen.findByText('READY', {}, PR_CHAIN)
    const before = fetchMock.mock.calls.filter((c) => String(c[0]).includes('/pr-status')).length
    fireEvent.click(refresh)
    await waitFor(() => {
      const after = fetchMock.mock.calls.filter((c) => String(c[0]).includes('/pr-status')).length
      expect(after).toBeGreaterThan(before)
    })
  })

  it('opens a PR session with the live title and number', async () => {
    mountPage()
    await screen.findByText('READY', {}, PR_CHAIN)
    fireEvent.click(screen.getAllByRole('button', { name: 'Discuss this pull request' })[0])
    expect(openSession).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'pr',
        id: 9,
        repo: 'zzq/fabric',
        url: 'https://github.com/zzq/fabric/pull/9',
        title: expect.stringContaining('Tune the cache'),
      }),
    )
  })
})

describe('AutoImprovementPage findings ledger', () => {
  it('links a committed finding to its commit built from the bare sha', async () => {
    mountPage()
    const link = await screen.findByRole('link', { name: 'View commit' })
    expect(link.getAttribute('href')).toBe('https://github.com/zzq/fabric/commit/abc1234')
    expect(screen.getByText('Committed to the branch')).toBeTruthy()
  })

  it('shows the raw token for a status the catalog does not know', async () => {
    mountPage()
    expect(await screen.findByText('mystery')).toBeTruthy()
  })

  it('names the repository and the branch with the origin prefix stripped', async () => {
    mountPage()
    expect(await screen.findByText('main')).toBeTruthy()
    expect(screen.getByText('zzq/fabric')).toBeTruthy()
  })

  it('publishes a queued change only after the operator confirms the branch', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mountPage()
    await screen.findByText('zzq/fabric')
    const commit = (await screen.findAllByRole('button', { name: 'Commit to the branch' }))[0]
    fireEvent.click(commit)
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('main'))
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => String(c[0]).endsWith('/findings/f1/commit') && (c[1] as RequestInit)?.method === 'POST')).toBe(true),
    )
  })

  it('sends nothing when the operator cancels the confirmation', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    mountPage()
    await screen.findByText('zzq/fabric')
    const commit = (await screen.findAllByRole('button', { name: 'Commit to the branch' }))[0]
    fireEvent.click(commit)
    expect(confirm).toHaveBeenCalledTimes(1)
    // The mutation's POST runs after its async onMutate, so let pending work settle before asserting absence.
    await act(async () => {})
    expect(fetchMock.mock.calls.some((c) => (c[1] as RequestInit | undefined)?.method === 'POST')).toBe(false)
  })

  it('opens a finding session with its fingerprint and PR link', async () => {
    mountPage()
    await screen.findByText('zzq/fabric')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Discuss this finding' }))[0])
    expect(openSession).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: 'finding',
        id: 'f1',
        repo: 'zzq/fabric',
        prompt: expect.stringContaining('/pull/9'),
      }),
    )
  })

  it('expands a finding row to reveal its details', async () => {
    mountPage()
    const toggle = (await screen.findAllByRole('button', { name: 'Show finding details' }))[2]
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    await waitFor(() => expect(toggle.getAttribute('aria-expanded')).toBe('true'))
    expect(await screen.findByText('nil deref in cache get')).toBeTruthy()
  })
})
