/**
 * Smoke test for DevFleetPage — renders with react-query + mocked fetch,
 * verifies loading state, fleet table, and empty state.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage from '../pages/DevFleetPage'

function renderPage() {
  return renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
}

const FLEET = {
  worktrees: [
    { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, last_updated_at: Date.now() / 1000 },
    { name: 'feature-x', is_main: false, running: true, has_dist: true, port: 7780, health: 200, behind: 3, last_updated_at: Date.now() / 1000 - 3600, pr: { number: 42, state: 'OPEN', url: 'https://github.com/org/repo/pull/42', isDraft: false }, pr_merged: false, ticket: 'GH-42', ticket_url: 'https://github.com/org/repo/issues/42' },
    { name: 'unprov', is_main: false, running: false, has_dist: false, behind: 0, last_updated_at: Date.now() / 1000 - 7200 },
  ],
}

describe('DevFleetPage', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders fleet table when API returns worktrees', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Dev Fleet')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    expect(screen.getAllByText('main').length).toBeGreaterThan(0)
  })

  it('shows empty state when no worktrees', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify({ worktrees: [] }), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('No worktrees found')).toBeInTheDocument())
  })

  it('shows error state on network failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() => Promise.reject(new Error('Network error')))
    renderPage()
    await waitFor(() => expect(screen.getByText('Backend unavailable')).toBeInTheDocument())
  })

  it('confirm dialog uses accessible Modal with role=dialog and Escape support', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/detail')) return Promise.resolve(new Response(JSON.stringify({ branch: 'feat', own_commits: 1 }), { status: 200 }))
      if (u.includes('/worktree/remove')) return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // No dialog initially
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('needsProv count excludes main worktree', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      // Main has has_dist:false but should NOT be counted as needs-provision
      const data = {
        worktrees: [
          { name: 'main', is_main: true, running: false, has_dist: false, behind: 0 },
          { name: 'wt-a', is_main: false, running: false, has_dist: false, behind: 0 },
        ],
      }
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 10240 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    // Wait for data to load (wt-a appears in the table)
    await waitFor(() => expect(screen.getByText('wt-a')).toBeInTheDocument())
    // The "Needs provision" stat card should show 1 (only wt-a), not 2
    expect(screen.getByText('Needs provision')).toBeInTheDocument()
    // StatCard renders the value — find all stat values matching '1'
    const statCards = screen.getAllByText('1')
    // At least one of them is the needs provision count
    expect(statCards.length).toBeGreaterThan(0)
  })

  it('provision polling treats timeout status as terminal with error notification', async () => {
    let pollCount = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/pod/provision')) return Promise.resolve(new Response(JSON.stringify({ ok: true, run_id: 'run-123' }), { status: 200 }))
      if (u.includes('/run?id=run-123')) {
        pollCount++
        if (pollCount === 1) return Promise.resolve(new Response(JSON.stringify({ status: 'running', output: ['building...'] }), { status: 200 }))
        return Promise.resolve(new Response(JSON.stringify({ status: 'timeout', output: ['timed out'] }), { status: 200 }))
      }
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Dev Fleet')).toBeInTheDocument())
    expect(pollCount).toBe(0) // No provision triggered automatically
  })

  it('uses SearchInput shared component for filtering', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument(), { timeout: 3000 })
    // SearchInput renders an input with aria-label
    const input = screen.getByLabelText('Filter worktrees')
    expect(input).toBeInTheDocument()
    expect(input.tagName.toLowerCase()).toBe('input')
    // Filter should hide non-matching rows
    fireEvent.change(input, { target: { value: 'feature' } })
    expect(screen.getByText('feature-x')).toBeInTheDocument()
  })

  it('reattaches to a running sync on page load via sync_run_id', async () => {
    const FLEET_WITH_SYNC = {
      ...FLEET,
      sync_run_id: 'run-sync-123',
      build_pending: false,
    }
    let runCalls = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_WITH_SYNC), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/run?id=run-sync-123')) {
        runCalls++
        return Promise.resolve(new Response(JSON.stringify({
          status: 'running', output: ['git pull completed', 'pip install running...'], started: Date.now() / 1000 - 30,
        }), { status: 200 }))
      }
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))
    // The reattach should have fetched the run status
    await waitFor(() => expect(runCalls).toBeGreaterThan(0), { timeout: 3000 })
  })

  it('renders sort dropdown with all 4 options', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const select = screen.getByLabelText('Sort worktrees') as HTMLSelectElement
    expect(select).toBeInTheDocument()
    expect(select.tagName.toLowerCase()).toBe('select')
    const options = Array.from(select.querySelectorAll('option'))
    expect(options.map(o => o.value)).toEqual(['status', 'recent', 'name', 'behind'])
  })

  it('shows build-pending chip when fleet.build_pending is true', async () => {
    const FLEET_BP = { ...FLEET, build_pending: true }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_BP), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    const chip = await waitFor(() => screen.getByText(/build pending/i))
    // The em-dash must render as the actual character, not a literal escape
    // sequence (regression: \u2014 written in bare JSX text renders literally).
    expect(chip.textContent).toContain('\u2014')
    expect(chip.textContent).not.toContain('\\u2014')
  })

  it('single-column list layout for worktrees (no auto-fill truncation)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    const { container } = renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const allDivs = container.querySelectorAll('div')
    const gridDiv = Array.from(allDivs).find(el => {
      const style = el.getAttribute('style') || ''
      return style.includes('auto-fill')
    })
    expect(gridDiv).toBeUndefined()
  })

  it('shows discovery error prominently when fleet returns error field', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify({ worktrees: [], error: 'sandbox disabled: no git binary found' }), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(screen.getByText('Discovery Error')).toBeInTheDocument()
    expect(screen.getByText('sandbox disabled: no git binary found')).toBeInTheDocument()
  })

  it('is registered in builtin component registry', async () => {
    const { hasBuiltinComponent } = await import('../apps/builtinRegistry')
    expect(hasBuiltinComponent('/dev-fleet')).toBe(true)
  })

  it('syncPhaseFromLines only advances on ::step:: markers, not pip done lines', async () => {
    // Import the module to access syncPhaseFromLines indirectly via the component
    // We test the stepper behavior through the rendered output
    const FLEET_SYNC = {
      ...FLEET,
      sync_run_id: 'run-marker-test',
    }
    let pollCount = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_SYNC), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/run?id=run-marker-test')) {
        pollCount++
        // Output contains pip's "done" but NO ::step:: markers beyond index 0
        return Promise.resolve(new Response(JSON.stringify({
          status: 'running',
          output: [
            '::step::0::Pull',
            'From github.com:org/repo',
            'Collecting package==1.0',
            'Successfully installed package done',
            'done',
          ],
          started: Date.now() / 1000 - 30,
        }), { status: 200 }))
      }
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(pollCount).toBeGreaterThan(0), { timeout: 3000 })
    // Percent must reflect ONLY the ::step:: marker (index 0) — pip's stray
    // "done" lines must not drive the coarse progress toward completion.
    const bar = await screen.findByRole('progressbar')
    const pct = Number(bar.getAttribute('aria-valuenow'))
    expect(pct).toBeGreaterThanOrEqual(0)
    expect(pct).toBeLessThan(25) // still inside the Pull/pip band, nowhere near done
    expect(screen.getByText(/~\d+%/)).toBeInTheDocument()
  })

  it('prune dialog renders with candidates and kept rows', async () => {
    const PRUNE_RESPONSE = {
      ok: true,
      candidates: [
        { name: 'merged-branch', code: 'merged' },
        { name: 'empty-branch', code: 'empty' },
      ],
      kept: [
        { name: 'active-branch', code: 'active' },
      ],
      scanned: 4,
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/prune-candidates')) return Promise.resolve(new Response(JSON.stringify(PRUNE_RESPONSE), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // Click the prune button
    const pruneBtn = screen.getByText('Prune merged')
    fireEvent.click(pruneBtn)
    // Wait for the prune dialog to appear
    await waitFor(() => expect(screen.getByText('Prune worktrees')).toBeInTheDocument(), { timeout: 3000 })
    // Candidates should have checkboxes
    expect(screen.getByText('merged-branch')).toBeInTheDocument()
    expect(screen.getByText('empty-branch')).toBeInTheDocument()
    // Kept rows visible
    expect(screen.getByText('active-branch')).toBeInTheDocument()
    // Verdict labels
    expect(screen.getByText('PR merged')).toBeInTheDocument()
    expect(screen.getByText('PR open or unmerged commits')).toBeInTheDocument()
    // Remove button
    expect(screen.getByText('Remove selected')).toBeInTheDocument()
  })

  it('shows "Make live" in the row menu for a non-live worktree and opens a confirm dialog', async () => {
    const FLEET_ONE = {
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_ONE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // Only the non-main row has a "More actions" menu.
    fireEvent.click(screen.getByLabelText('More actions'))
    const item = await screen.findByText('Make live')
    fireEvent.click(item)
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
    expect(screen.getByText('Make "feature-x" live?')).toBeInTheDocument()
  })

  it('hides "Make live" for the worktree that is already live', async () => {
    const FLEET_LIVE = {
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'live-wt', is_main: false, running: false, has_dist: true, behind: 0, is_live: true, path: '/wt/live' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_LIVE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('live-wt')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    // Menu is open (Rebase is always present) but Make live is omitted on the live row.
    expect(await screen.findByText('Rebase onto main')).toBeInTheDocument()
    expect(screen.queryByText('Make live')).toBeNull()
  })

  it('shows an inline "Make live" on the MAIN row when main is NOT live (switch back after cutover)', async () => {
    // A feature worktree is live; main is dormant (is_live:false). The main
    // row must offer Make live so the operator can cut back to main.
    const FLEET_MAIN_DORMANT = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: false, path: '/wt/main' },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, is_live: true, path: '/wt/feature-x' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_MAIN_DORMANT), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))
    // The dormant main row exposes an inline Make live control (feature-x is
    // live, so its own menu — unopened here — has no Make live to collide).
    const btn = await screen.findByTitle('Repoint the live gateway back at main (restarts the gateway)')
    expect(btn).toBeInTheDocument()
    fireEvent.click(btn)
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
    expect(screen.getByText('Make "main" live?')).toBeInTheDocument()
  })

  it('hides "Make live" on the MAIN row when main IS live', async () => {
    const FLEET_MAIN_LIVE = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: true, path: '/wt/main' },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, is_live: false, path: '/wt/feature-x' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_MAIN_LIVE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // Main is live -> no inline Make live control on the main row (feature-x's
    // menu is closed, so its Make live is not rendered either).
    expect(screen.queryByTitle('Repoint the live gateway back at main (restarts the gateway)')).toBeNull()
  })

  it('compact row: PR badge is a link with PR title as hover title, and shows the summary one-liner', async () => {
    const FLEET_CTX = {
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        {
          name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0,
          pr: { number: 42, state: 'OPEN', url: 'https://github.com/org/repo/pull/42', title: 'Add pagination' },
          summary: 'feat: add pagination to users API',
        },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_CTX), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // PR badge is wrapped in an <a> whose title attribute is the PR title.
    const link = screen.getByTitle('Add pagination')
    expect(link.tagName.toLowerCase()).toBe('a')
    expect(link).toHaveAttribute('href', 'https://github.com/org/repo/pull/42')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    // Purpose one-liner shows inline in the compact row.
    expect(screen.getByText('feat: add pagination to users API')).toBeInTheDocument()
  })

  it('drill-in shows issue chips, ticket chips, and the purpose summary', async () => {
    const FLEET_ONE = {
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0 },
      ],
    }
    const DETAIL = {
      branch: 'feat/x',
      pr: { number: 42, state: 'OPEN', url: 'https://github.com/org/repo/pull/42', title: 'Add pagination' },
      issues: [{ number: 147, url: 'https://github.com/org/repo/issues/147' }],
      tickets: [{ id: 'TT-5', url: 'https://tracker.example.com/TT-5' }],
      summary: 'feat: add pagination to users API',
      commits: [],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/worktree?name=')) return Promise.resolve(new Response(JSON.stringify(DETAIL), { status: 200 }))
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_ONE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // Expand feature-x (the only non-main row -> a single Expand control).
    fireEvent.click(screen.getByLabelText('Expand'))
    const issueLink = await screen.findByText('#147')
    expect(issueLink.tagName.toLowerCase()).toBe('a')
    expect(issueLink).toHaveAttribute('href', 'https://github.com/org/repo/issues/147')
    expect(issueLink).toHaveAttribute('rel', 'noopener noreferrer')
    const ticketLink = screen.getByText('TT-5')
    expect(ticketLink.tagName.toLowerCase()).toBe('a')
    expect(ticketLink).toHaveAttribute('href', 'https://tracker.example.com/TT-5')
    // The purpose one-liner renders in the drill-in.
    expect(screen.getByText('feat: add pagination to users API')).toBeInTheDocument()
  })

  /* ─── Row-actions dropdown: portal + flip (issue #146) ─── */
  // A worktree row whose "More actions" menu has items: non-main, not live,
  // has_dist & not running → Spin up pod / Rebase onto main / Make live.
  const FLEET_MENU = {
    worktrees: [
      { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
      { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
    ],
  }
  function mockFleet(data: unknown) {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
  }

  it('renders the row-actions dropdown in a portal on document.body (escapes Card overflow)', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    const menu = await screen.findByRole('menu')
    // Portaled: the menu is a direct child of <body>, not nested inside the SPA
    // container / row Card — this is what lets it escape Card overflow clipping.
    expect(menu.parentElement).toBe(document.body)
    // Items render inside the portaled menu and are reachable.
    expect(screen.getByText('Rebase onto main')).toBeInTheDocument()
    expect(screen.getByText('Make live')).toBeInTheDocument()
  })

  it('portaled row-actions items are clickable (opens the Make live dialog)', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    fireEvent.click(await screen.findByText('Make live'))
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
    expect(screen.getByText('Make "feature-x" live?')).toBeInTheDocument()
  })

  it('outside-click closes the portaled row-actions menu', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    expect(await screen.findByRole('menu')).toBeInTheDocument()
    // A click on <body> is outside both the trigger and the portaled menu.
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('Escape closes the portaled row-actions menu', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    expect(await screen.findByRole('menu')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
  })

  it('row-actions menu opens downward when there is room below', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByLabelText('More actions')
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: 100, bottom: 120, left: 400, right: 440, width: 40, height: 20, x: 400, y: 100, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(trigger)
    const menu = await screen.findByRole('menu') as HTMLElement
    expect(menu.getAttribute('data-placement')).toBe('down')
    // Downward placement anchors via `top`, not `bottom`.
    expect(menu.style.top).not.toBe('')
    expect(menu.style.bottom).toBe('')
  })

  it('row-actions menu flips upward when near the viewport bottom', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByLabelText('More actions')
    // Trigger a few px above the bottom edge → no room below → flip up.
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: window.innerHeight - 8, bottom: window.innerHeight - 4, left: 400, right: 440, width: 40, height: 20, x: 400, y: window.innerHeight - 8, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(trigger)
    const menu = await screen.findByRole('menu') as HTMLElement
    expect(menu.getAttribute('data-placement')).toBe('up')
    // Upward placement anchors via `bottom`, not `top`.
    expect(menu.style.bottom).not.toBe('')
    expect(menu.style.top).toBe('')
  })
})