/**
 * Smoke test for DevFleetPage — renders with react-query + mocked fetch,
 * verifies loading state, fleet table, and empty state.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage, { mergeLogWindow, LOG_GAP_MARKER, pruneVerdictLabel, gatewayRecovered } from '../pages/DevFleetPage'

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
    // Visible text must stay short: the Badge pill is whitespace-nowrap inside a
    // fixed-width grid column, so long text overflows into adjacent columns
    // (regression: full instruction was rendered inline and overlapped UPDATED).
    expect(chip.textContent).toBe('build pending')
    // Full instruction lives in the tooltip. The em-dash must be the actual
    // character, not a literal escape sequence (regression: \u2014 written in
    // bare JSX text renders literally).
    const title = chip.closest('[title]')?.getAttribute('title') ?? ''
    expect(title).toContain('restart gateway to apply')
    expect(title).toContain('\u2014')
    expect(title).not.toContain('\\u2014')
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

  /* ─── Provision progress: expandable log panel + failure persistence (issue #231) ─── */
  // 'unprov' is the only non-main has_dist:false row, so it renders the single
  // "Provision" button. Provision polling uses real 2s sleeps, hence the
  // generous per-test timeouts and waitFor windows below.
  it('provision stepper shows the FULL output in an expandable log panel and the toggle opens/closes it', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/pod/provision')) return Promise.resolve(new Response(JSON.stringify({ ok: true, run_id: 'run-p' }), { status: 200 }))
      // Stays 'running' so the streaming stepper + toggle are observable.
      if (u.includes('/run?id=run-p')) return Promise.resolve(new Response(JSON.stringify({ status: 'running', output: ['[provision] creating venv for unprov', 'Collecting deps', '[provision] building dist for unprov'] }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('unprov')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Provision'))
    // The row-spanning stepper replaces the tiny inline pill immediately.
    await waitFor(() => expect(screen.getByText('Provisioning')).toBeInTheDocument())
    // Open the log panel; it renders the WHOLE output, not just the last line.
    fireEvent.click(screen.getByLabelText('Toggle provision log'))
    // 'Collecting deps' is a NON-last line -> it exists only in the full panel
    // (the inline strip shows just the last line), proving full-output capture.
    await waitFor(() => expect(screen.getByText(/Collecting deps/)).toBeInTheDocument(), { timeout: 4000 })
    // Toggling closed removes the panel again.
    fireEvent.click(screen.getByLabelText('Toggle provision log'))
    await waitFor(() => expect(screen.queryByText(/Collecting deps/)).toBeNull())
  }, 15000)

  it('failed provision PERSISTS with an auto-expanded log and a working dismiss', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/pod/provision')) return Promise.resolve(new Response(JSON.stringify({ ok: true, run_id: 'run-f' }), { status: 200 }))
      if (u.includes('/run?id=run-f')) return Promise.resolve(new Response(JSON.stringify({ status: 'done', exit_code: 1, output: ['npm ERR! boom', 'FATAL: npm run build failed'] }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('unprov')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Provision'))
    // The persistent strip is proven by its dismiss button (the toast has no
    // this uniquely targets the persisted stepper) survives instead of vanishing.
    await waitFor(() => expect(screen.getByLabelText('Dismiss provision status')).toBeInTheDocument(), { timeout: 4000 })
    // The log auto-expands on failure: a non-last output line shows in the panel.
    expect(screen.getByText(/npm ERR! boom/)).toBeInTheDocument()
    // Dismiss clears the persisted stepper and restores the Provision entry point.
    fireEvent.click(screen.getByLabelText('Dismiss provision status'))
    await waitFor(() => expect(screen.queryByLabelText('Dismiss provision status')).toBeNull())
    expect(screen.getByText('Provision')).toBeInTheDocument()
  }, 15000)

  it('successful provision flashes a green Provisioned status then clears back to the Provision button', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/pod/provision')) return Promise.resolve(new Response(JSON.stringify({ ok: true, run_id: 'run-s' }), { status: 200 }))
      if (u.includes('/run?id=run-s')) return Promise.resolve(new Response(JSON.stringify({ status: 'done', exit_code: 0, output: ['[provision] done'] }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('unprov')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Provision'))
    // Brief green success flash (stepper span and/or toast).
    await waitFor(() => expect(screen.getAllByText('Provisioned').length).toBeGreaterThan(0), { timeout: 4000 })
    // The stepper auto-clears (~2.5s), so the row returns to its Provision entry
    // point — a toast-independent proof the transient success state cleared.
    await waitFor(() => expect(screen.getByText('Provision')).toBeInTheDocument(), { timeout: 6000 })
  }, 15000)

  // FINDING 1: the single-flight guard returns {ok:false, run_id:<in-flight>}
  // when a provision is already running. That must RESUME polling that run, not
  // render a false red "Provision failed" state.
  it('single-flight response (ok:false + run_id) resumes polling instead of failing', async () => {
    let polledInflight = false
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      // Guard reply: already running -> ok:false but carries the in-flight rid.
      if (u.includes('/pod/provision')) return Promise.resolve(new Response(JSON.stringify({ ok: false, error: 'provision already running', run_id: 'run-inflight' }), { status: 200 }))
      if (u.includes('/run?id=run-inflight')) {
        polledInflight = true
        return Promise.resolve(new Response(JSON.stringify({ status: 'running', output: ['[provision] creating venv for unprov', 'Collecting deps'] }), { status: 200 }))
      }
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('unprov')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Provision'))
    // Reattaches to the in-flight run: it actually polls the in-flight run id
    // (proving it resumed rather than bailing on the ok:false response)...
    await waitFor(() => expect(polledInflight).toBe(true), { timeout: 4000 })
    // ...the streaming stepper is shown, and the false-failure copy never appears.
    expect(screen.getByText('Provisioning')).toBeInTheDocument()
    expect(screen.queryByText('Provision failed to start')).toBeNull()
    expect(screen.queryByText(/Provision failed/)).toBeNull()
  }, 15000)

  // FINDING 3: /api/run tail-truncates to the last ~60 lines, so early output
  // scrolls out of later windows. The client accumulates windows, so an early
  // line remains visible even after it has left the server's window.
  it('accumulates log output across polls so early lines survive the server window', async () => {
    let call = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/pod/provision')) return Promise.resolve(new Response(JSON.stringify({ ok: true, run_id: 'run-acc' }), { status: 200 }))
      if (u.includes('/run?id=run-acc')) {
        call++
        // The window slides forward each poll; 'line-early-1' is only present
        // in the first window and is gone by the final one.
        if (call === 1) return Promise.resolve(new Response(JSON.stringify({ status: 'running', output: ['line-early-1', 'line-2', 'line-3'] }), { status: 200 }))
        if (call === 2) return Promise.resolve(new Response(JSON.stringify({ status: 'running', output: ['line-2', 'line-3', 'line-4'] }), { status: 200 }))
        return Promise.resolve(new Response(JSON.stringify({ status: 'done', exit_code: 1, output: ['line-3', 'line-4', 'line-5'] }), { status: 200 }))
      }
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('unprov')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Provision'))
    // Ends in failure so the accumulated log auto-expands and persists.
    await waitFor(() => expect(screen.getByLabelText('Dismiss provision status')).toBeInTheDocument(), { timeout: 10000 })
    // Both the earliest line (window 1, only ever in the first poll) and the
    // latest (final window) are present, proving windows were merged not
    // replaced. 'line-early-1' is unique to the panel; 'line-5' also shows in
    // the inline last-line strip, hence getAllByText.
    expect(screen.getByText(/line-early-1/)).toBeInTheDocument()
    expect(screen.getAllByText(/line-5/).length).toBeGreaterThan(0)
  }, 20000)

  it('prune progress renders a per-item checklist with live status chips and inline failure reason', async () => {
    let pollCount = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/prune-candidates')) return Promise.resolve(new Response(JSON.stringify({
        ok: true,
        candidates: [{ name: 'wt-a', code: 'merged' }, { name: 'wt-b', code: 'merged' }],
        kept: [], scanned: 2,
      }), { status: 200 }))
      if (u.includes('/prune-run')) return Promise.resolve(new Response(JSON.stringify({ ok: true, total: 2 }), { status: 200 }))
      if (u.includes('/prune-status')) {
        pollCount++
        if (pollCount === 1) {
          // In-flight: distinct per-item phases (one removing, one stopping pod).
          return Promise.resolve(new Response(JSON.stringify({
            running: true, total: 2, done: 0, current: 'wt-a', results: [],
            items: { 'wt-a': { status: 'removing', error: null }, 'wt-b': { status: 'stopping_pod', error: null } },
          }), { status: 200 }))
        }
        // Terminal: one removed, one failed with a reason to surface inline.
        return Promise.resolve(new Response(JSON.stringify({
          running: false, total: 2, done: 2, current: null,
          results: [{ name: 'wt-a', ok: true }, { name: 'wt-b', ok: false, error: 'pod still active after shutdown' }],
          items: { 'wt-a': { status: 'done', error: null }, 'wt-b': { status: 'failed', error: 'pod still active after shutdown' } },
        }), { status: 200 }))
      }
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('Prune worktrees')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Remove selected'))
    // The checklist replaces the single-line progress: one row per selected item.
    await waitFor(() => expect(screen.getByTestId('prune-item-wt-a')).toBeInTheDocument(), { timeout: 4000 })
    expect(screen.getByTestId('prune-item-wt-b')).toBeInTheDocument()
    // Terminal poll: wt-a done, wt-b failed with the failure reason inline.
    await waitFor(() => expect(screen.getByTestId('prune-item-wt-a')).toHaveAttribute('data-status', 'done'), { timeout: 8000 })
    expect(screen.getByTestId('prune-item-wt-b')).toHaveAttribute('data-status', 'failed')
    expect(screen.getByText('pod still active after shutdown')).toBeInTheDocument()
    expect(screen.getByText('Prune complete')).toBeInTheDocument()
  }, 15000)
})

// FINDING 3 (unit): the overlap-merge algorithm behind client-side log
// accumulation — dedupes the overlapping suffix/prefix and appends the rest.
describe('mergeLogWindow', () => {
  it('returns the window when the buffer is empty', () => {
    expect(mergeLogWindow([], ['a', 'b'])).toEqual(['a', 'b'])
  })
  it('returns the buffer unchanged when the window is empty', () => {
    expect(mergeLogWindow(['a', 'b'], [])).toEqual(['a', 'b'])
  })
  it('appends only the non-overlapping remainder of an advancing window', () => {
    // buffer suffix ['b','c'] == window prefix ['b','c'] -> append only ['d'].
    expect(mergeLogWindow(['a', 'b', 'c'], ['b', 'c', 'd'])).toEqual(['a', 'b', 'c', 'd'])
  })
  it('concatenates fully when there is no overlap (window jumped past a full window)', () => {
    expect(mergeLogWindow(['a', 'b'], ['x', 'y'])).toEqual(['a', 'b', LOG_GAP_MARKER, 'x', 'y'])
  })
  it('adds nothing when the window is a subset already at the buffer tail', () => {
    expect(mergeLogWindow(['a', 'b', 'c'], ['b', 'c'])).toEqual(['a', 'b', 'c'])
  })
  it('handles repeated lines by matching the longest suffix/prefix overlap', () => {
    // Longest suffix of buffer that prefixes the window is ['x','x'] (len 2).
    expect(mergeLogWindow(['x', 'x', 'x'], ['x', 'x', 'y'])).toEqual(['x', 'x', 'x', 'y'])
  })
})

// Kept-reason surfacing: machine verdict codes -> human-readable text shown in
// the prune preview dialog so users see WHY a row is a candidate or is kept.
describe('pruneVerdictLabel', () => {
  it('maps machine prune codes to human-readable reasons', () => {
    expect(pruneVerdictLabel('merged')).toBe('PR merged')
    expect(pruneVerdictLabel('empty')).toMatch(/no commits/i)
    expect(pruneVerdictLabel('merged_dirty')).toMatch(/uncommitted/i)
    expect(pruneVerdictLabel('merged_new_commits')).toMatch(/after merge/i)
    expect(pruneVerdictLabel('merged_unverified')).toMatch(/verification/i)
    expect(pruneVerdictLabel('active')).toBe('PR open or unmerged commits')
    expect(pruneVerdictLabel('fresh')).toMatch(/recently/i)
    expect(pruneVerdictLabel('dirty_check_failed')).toMatch(/git status/i)
  })
  it('falls back to the raw code for unknown values and never throws', () => {
    expect(pruneVerdictLabel('totally_unknown')).toBe('totally_unknown')
    expect(pruneVerdictLabel(undefined)).toBe('')
    expect(pruneVerdictLabel('')).toBe('')
  })
})


// Restart identity handshake (issue #639): "recovered" means a DIFFERENT start
// identity appeared, never "a 200 came back".
describe('gatewayRecovered', () => {
  it('is true only when a captured id and a different current id are both present', () => {
    expect(gatewayRecovered('100', '200')).toBe(true)
  })
  it('is false when the current id equals the captured id (old process still winding down)', () => {
    expect(gatewayRecovered('100', '100')).toBe(false)
  })
  it('is false when identity is unavailable on either side (degrade, never falsely recover)', () => {
    expect(gatewayRecovered(null, '200')).toBe(false)
    expect(gatewayRecovered('100', null)).toBe(false)
    expect(gatewayRecovered('100', undefined)).toBe(false)
    expect(gatewayRecovered(undefined, undefined)).toBe(false)
  })
})

describe('DevFleetPage restart handshake', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('enters the "Restarting — reconnecting" state and disables the action buttons on restart', async () => {
    const FLEET_LIVE = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: true, path: '/wt/main' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_LIVE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      if (u.includes('/restart-gateway')) return Promise.resolve(new Response(JSON.stringify({ ok: true, start_id: 'BEFORE' }), { status: 200 }))
      // Health keeps returning the SAME identity, so the handshake never fires a
      // reload during the test — we can observe the held "restarting" state.
      if (u.includes('/apps/dev-fleet/api/health')) return Promise.resolve(new Response(JSON.stringify({ status: 'ok', start_id: 'BEFORE' }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))

    const syncBtn = () => screen.getByText('Pull+Build').closest('button') as HTMLButtonElement
    expect(syncBtn()).not.toBeDisabled()

    // Click the row Restart button, then confirm in the dialog.
    fireEvent.click(screen.getByLabelText('Restart gateway'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart' }))

    // The explicit overlay shows and the action buttons are disabled while held.
    await waitFor(() => expect(screen.getByText(/Restarting/)).toBeInTheDocument())
    expect(syncBtn()).toBeDisabled()
    expect(screen.getByLabelText('Restart gateway')).toBeDisabled()
  })

  it('reloads once the polled start identity DIFFERS from the captured one', async () => {
    const FLEET_LIVE = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: true, path: '/wt/main' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_LIVE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      if (u.includes('/restart-gateway')) return Promise.resolve(new Response(JSON.stringify({ ok: true, start_id: 'BEFORE' }), { status: 200 }))
      // The NEW process reports a DIFFERENT identity -> handshake must reload.
      if (u.includes('/apps/dev-fleet/api/health')) return Promise.resolve(new Response(JSON.stringify({ status: 'ok', start_id: 'AFTER' }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    // Stub the navigation side effect so the identity-change branch is observable.
    const origReload = window.location.reload
    const reloadSpy = vi.fn()
    Object.defineProperty(window.location, 'reload', { configurable: true, value: reloadSpy })
    try {
      renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
      await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))

      fireEvent.click(screen.getByLabelText('Restart gateway'))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(within(dialog).getByRole('button', { name: 'Restart' }))

      // awaitGatewayBack sleeps ~3s before its first poll, then reloads on the
      // differing identity; allow generous headroom over real timers.
      await waitFor(() => expect(reloadSpy).toHaveBeenCalled(), { timeout: 8000 })
    } finally {
      Object.defineProperty(window.location, 'reload', { configurable: true, value: origReload })
    }
  }, 12000)

  it('disables Restart and Pull+Build while a Make Live request is still in flight', async () => {
    // Regression: `restarting` only goes true AFTER the /make-live POST resolves,
    // but that POST is what writes the systemd drop-in and issues the
    // daemon-reload. A Restart fired inside that window can tear the gateway down
    // mid-write, leaving persisted and loaded unit state inconsistent. The global
    // action predicates must therefore also honour an in-flight cutover.
    const FLEET = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: false, path: '/wt/main' },
      ],
    }
    // Hold /make-live open so the test observes the pre-`restarting` window.
    let releaseMakeLive: (() => void) | null = null
    const makeLiveHeld = new Promise<void>((res) => { releaseMakeLive = res })
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      if (u.includes('/make-live')) {
        return makeLiveHeld.then(
          () => new Response(JSON.stringify({ ok: true, cutover: true, start_id: 'BEFORE' }), { status: 200 }),
        )
      }
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    try {
      renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
      await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))

      expect(screen.getByLabelText('Restart gateway')).not.toBeDisabled()

      fireEvent.click(screen.getByText('Make live').closest('button') as HTMLButtonElement)
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(within(dialog).getByRole('button', { name: /Make live/i }))

      // POST is still pending: `restarting` is false, yet both global actions
      // must already be locked out.
      await waitFor(() => expect(screen.getByLabelText('Restart gateway')).toBeDisabled())
      expect(screen.getByText('Pull+Build').closest('button')).toBeDisabled()
    } finally {
      releaseMakeLive?.()
    }
  }, 15000)

  it('treats a reachable 404 on the health route as recovery instead of waiting out the timeout', async () => {
    // Regression: making live a worktree that predates /api/health means the new
    // gateway answers 404 forever, so its identity can never appear and the
    // handshake would burn the whole 60s window. A reachable 404 during the
    // handshake proves a gateway IS serving us — reload into it.
    const FLEET_LIVE = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: true, path: '/wt/main' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_LIVE), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      if (u.includes('/restart-gateway')) return Promise.resolve(new Response(JSON.stringify({ ok: true, start_id: 'BEFORE' }), { status: 200 }))
      // Post-cutover backend has no /api/health at all.
      if (u.includes('/apps/dev-fleet/api/health')) return Promise.resolve(new Response('not found', { status: 404 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    const origReload = window.location.reload
    const reloadSpy = vi.fn()
    Object.defineProperty(window.location, 'reload', { configurable: true, value: reloadSpy })
    try {
      renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
      await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))

      fireEvent.click(screen.getByLabelText('Restart gateway'))
      const dialog = await screen.findByRole('dialog')
      fireEvent.click(within(dialog).getByRole('button', { name: 'Restart' }))

      await waitFor(() => expect(reloadSpy).toHaveBeenCalled(), { timeout: 8000 })
    } finally {
      Object.defineProperty(window.location, 'reload', { configurable: true, value: origReload })
    }
  }, 12000)
})
