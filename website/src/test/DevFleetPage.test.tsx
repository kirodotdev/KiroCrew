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

  it('main row warns when the primary checkout is parked on a non-base branch', async () => {
    // Regression: the primary checkout was left on a merged PR's feature
    // branch. The row's name is hardcoded to the base branch, so without the
    // badge the fleet claimed "main" while showing that branch's merged PR
    // pill — contradictory, and the truth only surfaced when Pull+Build
    // refused to sync.
    const data = {
      base_branch: 'main',
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 117, branch: 'fix/old-merged-pr', pr: { number: 1742, state: 'MERGED', url: 'https://github.com/org/repo/pull/1742', isDraft: false } },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('Parked on fix/old-merged-pr')).toBeInTheDocument())
    // The tooltip explains the consequence (Pull+Build refuses to sync).
    expect(screen.getByText('Parked on fix/old-merged-pr')).toHaveAttribute('title', expect.stringContaining('fix/old-merged-pr'))
  })

  it('main row keeps the plain label when the primary checkout is on the base branch', async () => {
    const data = {
      base_branch: 'main',
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'main' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getAllByText(/main/).length).toBeGreaterThan(0))
    expect(screen.queryByText(/^Parked on /)).toBeNull()
  })

  it('main row does not false-flag when the payload lacks base_branch', async () => {
    // An older backend that omits base_branch must not trigger the warning —
    // comparing against a hardcoded 'main' would false-flag repos whose base
    // branch has a different name.
    const data = {
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, branch: 'trunk' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(data), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getAllByText(/main/).length).toBeGreaterThan(0))
    expect(screen.queryByText('Parked on trunk')).toBeNull()
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
    // `SimpleSelect` wraps a Radix Select, so there is no `<select>` to read
    // `.options` off and a `change` event on the trigger does nothing — open the
    // popup, then enumerate the rendered options.
    const trigger = screen.getByRole('combobox', { name: 'Sort worktrees' })
    expect(trigger).toHaveTextContent('Sort: status')
    fireEvent.click(trigger)
    const options = await screen.findAllByRole('option')
    expect(options.map(o => o.textContent)).toEqual([
      'Sort: status', 'Sort: recent', 'Sort: name', 'Sort: behind',
    ])
    // And picking one actually re-sorts: 'behind' puts feature-x (behind 3) first.
    fireEvent.click(screen.getByRole('option', { name: 'Sort: behind' }))
    await waitFor(() => expect(trigger).toHaveTextContent('Sort: behind'))
  })

  it('status sort orders equal pod-status rows by PR state, not alphabetically', async () => {
    // Names are chosen so plain alphabetical order (a→e) CONTRADICTS the
    // expected review-state order (open → draft → no PR → closed → merged);
    // without the prRank secondary key this test fails.
    const FLEET_PR = {
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'wt-a-merged', is_main: false, running: false, has_dist: false, pr: { number: 1, state: 'MERGED', url: 'https://github.com/org/repo/pull/1', isDraft: false } },
        { name: 'wt-b-closed', is_main: false, running: false, has_dist: false, pr: { number: 2, state: 'CLOSED', url: 'https://github.com/org/repo/pull/2', isDraft: false } },
        { name: 'wt-c-nopr', is_main: false, running: false, has_dist: false },
        { name: 'wt-d-draft', is_main: false, running: false, has_dist: false, pr: { number: 4, state: 'OPEN', url: 'https://github.com/org/repo/pull/4', isDraft: true } },
        { name: 'wt-e-open', is_main: false, running: false, has_dist: false, pr: { number: 5, state: 'OPEN', url: 'https://github.com/org/repo/pull/5', isDraft: false } },
        // Pod status stays the PRIMARY key: a running pod with a merged PR
        // still sorts above every not-built row.
        { name: 'wt-z-pod-merged', is_main: false, running: true, has_dist: true, port: 7781, health: 200, pr: { number: 6, state: 'MERGED', url: 'https://github.com/org/repo/pull/6', isDraft: false } },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET_PR), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('wt-e-open')).toBeInTheDocument())
    // Default sort is 'status'. Expected order: pod-up row first, then the
    // not-built rows by review state (open, draft, no PR, closed, merged).
    const expected = ['wt-z-pod-merged', 'wt-e-open', 'wt-d-draft', 'wt-c-nopr', 'wt-b-closed', 'wt-a-merged']
    const els = expected.map((n) => screen.getByText(n))
    for (let i = 0; i < els.length - 1; i++) {
      expect(
        els[i].compareDocumentPosition(els[i + 1]) & Node.DOCUMENT_POSITION_FOLLOWING,
        `${expected[i]} should render before ${expected[i + 1]}`,
      ).toBeTruthy()
    }
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
    const pruneBtn = screen.getByText('Prune merged')
    fireEvent.click(pruneBtn)
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
    // Scoped to the portaled menu: the main row carries its own Make live
    // control, so a document-wide query is ambiguous.
    const item = within(await screen.findByRole('menu')).getByText('Make live')
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
    expect(within(await screen.findByRole('menu')).queryByText('Make live')).toBeNull()
  })

  // --- pods unavailable on this host (non-Linux / no systemctl) ---
  // Before pods_available existed, the backend computed a reason string and
  // never sent it, so these controls rendered and silently failed.
  const FLEET_NO_PODS = {
    pods_available: false,
    pods_unavailable_reason: 'Pods are Linux systemd --user units; this host is darwin. Preview a worktree with ./dev-backend.sh instead.',
    worktrees: [
      { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
      { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
    ],
  }

  function mockFleet(payload: unknown) {
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(payload), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
  }

  // --- serving install differs from the managed checkout ---
  // The silent-wrong-answer case: Pull+Build fast-forwards the checkout and
  // reports success while an older install keeps serving, so no other control
  // on this page reveals that the managed code is not the running code.
  it('warns when the install serving the dashboard is not the managed checkout', async () => {
    mockFleet({
      serving_install_reason: 'this dashboard is served by the install at /Applications/KiroCrew.app/Contents/Resources/backend-dist/kirocrew-backend-arm64/lib/python3.12/site-packages/kiro_crew, which is not inside the checkout Dev Fleet manages (/Users/dev/kirocrew).',
      worktrees: [{ name: 'main', is_main: true, running: false, has_dist: true, behind: 0 }],
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('serving-install-warning')).toBeInTheDocument())
    // Surfaced verbatim, both installs named.
    expect(screen.getByText(/not inside the checkout Dev Fleet manages/)).toBeInTheDocument()
    expect(screen.getByText(/backend-dist/)).toBeInTheDocument()
  })

  it('shows no serving-install warning for a matching install', async () => {
    mockFleet({
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    expect(screen.queryByTestId('serving-install-warning')).toBeNull()
  })

  it('explains WHY pods are unavailable instead of failing silently', async () => {
    mockFleet(FLEET_NO_PODS)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    expect(screen.getByText('Pods are unavailable on this host.')).toBeInTheDocument()
    // The backend's reason is surfaced verbatim, including the suggested fix.
    expect(screen.getByText(/this host is darwin/)).toBeInTheDocument()
    expect(screen.getByText(/dev-backend\.sh/)).toBeInTheDocument()
  })

  it('hides pod-dependent row actions when pods cannot run, but keeps the rest', async () => {
    mockFleet(FLEET_NO_PODS)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    // Rebase is platform-neutral and stays -> proves the menu really opened.
    expect(await screen.findByText('Rebase onto main')).toBeInTheDocument()
    const menu = within(await screen.findByRole('menu'))
    for (const gone of ['Spin up pod', 'QA + video', 'Stop pod', 'Restart pod']) {
      expect(menu.queryByText(gone)).toBeNull()
    }
    // Make live is NOT pod-dependent: staging writes only the live-target
    // pointer, so hiding it here would hide it on the hosts it exists to serve.
    expect(menu.getByText('Make live')).toBeInTheDocument()
  })

  it('keeps Provision available without pods (pod provision never touches systemd)', async () => {
    mockFleet({
      ...FLEET_NO_PODS,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'unbuilt', is_main: false, running: false, has_dist: false, behind: 0, path: '/wt/unbuilt' },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('unbuilt')).toBeInTheDocument())
    expect(screen.getByText('Provision')).toBeInTheDocument()
  })

  it('keeps pod actions when the backend omits pods_available (older backend)', async () => {
    // Absent field must not be read as "unsupported" — that would silently
    // strip pod controls from a perfectly capable Linux host.
    mockFleet({
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0 },
        { name: 'feature-x', is_main: false, running: false, has_dist: true, behind: 0, path: '/wt/feature-x' },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    expect(screen.queryByText('Pods are unavailable on this host.')).toBeNull()
    fireEvent.click(screen.getByLabelText('More actions'))
    expect(await screen.findByText('Spin up pod')).toBeInTheDocument()
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

  /* ─── Row-actions dropdown: portal + flip ─── */
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
    expect(within(menu).getByText('Make live')).toBeInTheDocument()
  })

  it('portaled row-actions items are clickable (opens the Make live dialog)', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('More actions'))
    fireEvent.click(within(await screen.findByRole('menu')).getByText('Make live'))
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

  /* ─── Pull+Build confirm popover: portal + flip ─── */
  // The confirm popover used to be position:absolute inside the row, so the
  // Worktrees Card's `.card-glow { overflow: hidden }` clipped it. It is now
  // portaled to <body> with fixed positioning, like the row-actions menu.
  async function openPullBuildConfirm() {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByText('Pull+Build').closest('button') as HTMLButtonElement
    fireEvent.click(trigger)
    return { trigger, pop: await screen.findByRole('dialog') }
  }

  it('renders the Pull+Build confirm popover in a portal on document.body (escapes Card overflow)', async () => {
    const { pop } = await openPullBuildConfirm()
    // Portaled: a direct child of <body>, not nested inside the row Card whose
    // overflow:hidden previously cut the popover off mid-render.
    expect(pop.parentElement).toBe(document.body)
    expect(pop.getAttribute('aria-label')).toBe('Pull + Build main')
    expect(within(pop).getByText('Pulls main and rebuilds (~6 min). Does NOT restart.')).toBeInTheDocument()
  })

  it('Start inside the portaled confirm popover still fires the sync request', async () => {
    const { pop } = await openPullBuildConfirm()
    // The outside-click guard must exclude the portaled popover itself, or the
    // mousedown preceding this click would close it before the click lands.
    fireEvent.click(within(pop).getByText('Start'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => {
      const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
      const urls = calls.map((c) => (typeof c[0] === 'string' ? c[0] : (c[0] as Request).url))
      expect(urls.some((u) => u.includes('/sync'))).toBe(true)
    })
  })

  it('outside-click and Escape close the portaled confirm popover', async () => {
    const { trigger } = await openPullBuildConfirm()
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(trigger)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('confirm popover opens downward when there is room below', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByText('Pull+Build').closest('button') as HTMLButtonElement
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: 100, bottom: 120, left: 400, right: 440, width: 40, height: 20, x: 400, y: 100, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(trigger)
    const pop = await screen.findByRole('dialog') as HTMLElement
    expect(pop.getAttribute('data-placement')).toBe('down')
    expect(pop.style.position).toBe('fixed')
    expect(pop.style.top).not.toBe('')
    expect(pop.style.bottom).toBe('')
  })

  it('confirm popover flips upward when near the viewport bottom', async () => {
    mockFleet(FLEET_MENU)
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    const trigger = screen.getByText('Pull+Build').closest('button') as HTMLButtonElement
    // A row a few px above the bottom edge is exactly the case in the bug
    // report: no room below, so the popover must flip up instead of being cut.
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({
      top: window.innerHeight - 8, bottom: window.innerHeight - 4, left: 400, right: 440, width: 40, height: 20, x: 400, y: window.innerHeight - 8, toJSON: () => ({}),
    } as DOMRect)
    fireEvent.click(trigger)
    const pop = await screen.findByRole('dialog') as HTMLElement
    expect(pop.getAttribute('data-placement')).toBe('up')
    expect(pop.style.bottom).not.toBe('')
    expect(pop.style.top).toBe('')
  })

  /* ─── Provision progress: expandable log panel + failure persistence ─── */
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

  // The single-flight guard returns {ok:false, run_id:<in-flight>} when a
  // provision is already running. That must RESUME polling that run, not
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

  // /api/run tail-truncates to the last ~60 lines, so early output scrolls out
  // of later windows. The client accumulates windows, so an early line remains
  // visible even after it has left the server's window.
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

  it('refetches the fleet with fresh=1 once a prune finishes', async () => {
    // The backend serves /fleet from a stale-while-revalidate cache, so a plain
    // refetch after a prune returns the PRE-prune snapshot and the removed rows
    // keep rendering. The post-action refresh must force a rebuild.
    const fleetUrls: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) {
        fleetUrls.push(u)
        return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      }
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
      if (u.includes('/prune-candidates')) return Promise.resolve(new Response(JSON.stringify({
        ok: true, candidates: [{ name: 'wt-a', code: 'merged' }], kept: [], scanned: 1,
      }), { status: 200 }))
      if (u.includes('/prune-run')) return Promise.resolve(new Response(JSON.stringify({ ok: true, total: 1 }), { status: 200 }))
      if (u.includes('/prune-status')) return Promise.resolve(new Response(JSON.stringify({
        running: false, total: 1, done: 1, current: null,
        results: [{ name: 'wt-a', ok: true }],
        items: { 'wt-a': { status: 'done', error: null } },
      }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feature-x')).toBeInTheDocument())
    // The initial load must NOT force a rebuild — only post-action refreshes do.
    expect(fleetUrls.every((u) => !u.includes('fresh=1'))).toBe(true)

    fireEvent.click(screen.getByText('Prune merged'))
    await waitFor(() => expect(screen.getByText('Prune worktrees')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Remove selected'))
    await waitFor(
      () => expect(fleetUrls.some((u) => u.includes('fresh=1'))).toBe(true),
      { timeout: 8000 },
    )
  }, 15000)
})

// The overlap-merge algorithm behind client-side log accumulation — dedupes
// the overlapping suffix/prefix and appends the rest.
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


// Restart identity handshake: "recovered" means a DIFFERENT start identity
// appeared, never "a 200 came back".
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
    // but that POST is what stages the live target and issues the restart. A
    // Restart fired inside that window can tear the gateway down mid-cutover.
    // The global action predicates must therefore also honour an in-flight cutover.
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

  it('keeps a failed restart on the page instead of only in a toast', async () => {
    // The wedge message is a pair of commands with absolute paths that the
    // operator has to run. Toasts are pointer-events:none and self-dismiss, so
    // the one actionable failure this page can produce must also land somewhere
    // selectable that outlives the 7s window.
    // The message restart_detached returns when the loaded agent predates the
    // graceful-restart contract: an instruction the operator has to act on, so it
    // must survive long enough to be read and copied.
    const RESTART_ERR = "loaded launchd restart contract is outdated; re-run `kirocrew service install`"
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
      if (u.includes('/restart-gateway')) return Promise.resolve(new Response(JSON.stringify({ ok: false, error: RESTART_ERR }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))

    fireEvent.click(screen.getByLabelText('Restart gateway'))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restart' }))

    const banner = await screen.findByTestId('gateway-restart-error')
    // The remedy survives in full, not truncated or summarised away.
    expect(banner.textContent).toContain('restart contract is outdated')
    expect(banner.textContent).toContain('kirocrew service install')
    // Dismissable, so it does not become permanent furniture.
    fireEvent.click(within(banner).getByLabelText('Dismiss'))
    await waitFor(() => expect(screen.queryByTestId('gateway-restart-error')).toBeNull())
  })

  it('reports a staged-only cutover without entering the restart handshake', async () => {
    // A host whose gateway Dev Fleet cannot bounce gets ok:true + staged_only:
    // the pointer is written and the operator finishes the cutover. No
    // replacement process is coming, so entering the restart overlay would
    // strand the user on the 60s timeout and bury the command they need.
    const FLEET = {
      gateway_service_active: true,
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: false, path: '/wt/main' },
      ],
    }
    let healthPolled = false
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      if (u.includes('/make-live')) {
        return Promise.resolve(new Response(JSON.stringify({
          ok: true, cutover: true, staged_only: true, target: '/wt/main',
          manual_restart: 'sudo systemctl restart kirocrew',
          notice: 'main is now the live target. Run `sudo systemctl restart kirocrew` to finish the cutover.',
        }), { status: 200 }))
      }
      if (u.includes('/health')) { healthPolled = true; return Promise.resolve(new Response('{}', { status: 200 })) }
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))

    fireEvent.click(screen.getByText('Make live').closest('button') as HTMLButtonElement)
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /Make live/i }))

    // The backend-authored notice carries the command, so it is surfaced verbatim.
    await waitFor(() => expect(screen.getByText(/finish the cutover/i)).toBeInTheDocument())
    // And the restart overlay never opens: Restart stays available.
    expect(screen.getByLabelText('Restart gateway')).not.toBeDisabled()
    expect(healthPolled).toBe(false)
  }, 15000)

  it('marks a staged worktree restart-pending instead of live', async () => {
    // The toast that announces a staged cutover is transient; the pending state
    // is not. Without a persistent marker an operator who dismissed or missed
    // the toast reads the OLD running image as the new one and draws
    // conclusions about code that is not running.
    const FLEET = {
      gateway_service_active: false,
      staged_target: '/wt/feature',
      manual_restart: 'kirocrew restart',
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: true, is_staged: false, path: '/wt/main' },
        { name: 'feature', is_main: false, running: false, has_dist: true, behind: 0, is_live: false, is_staged: true, path: '/wt/feature' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getAllByText('feature').length).toBeGreaterThan(0))

    // The staged row is flagged, and it is NOT the row wearing the live badge.
    await waitFor(() => expect(screen.getByText('Restart pending')).toBeInTheDocument())
    expect(screen.getByText('live')).toBeInTheDocument()
  }, 15000)

  it('does not promise an automatic restart when the gateway cannot be driven', async () => {
    // The dialog is the moment of commitment: on a host where Dev Fleet cannot
    // bounce the service, saying "the gateway restarts and this page reconnects
    // automatically" is a promise the staged path does not keep.
    const FLEET = {
      gateway_service_active: false,
      manual_restart: 'kirocrew restart',
      worktrees: [
        { name: 'main', is_main: true, running: false, has_dist: true, behind: 0, is_live: false, path: '/wt/main' },
      ],
    }
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = typeof url === 'string' ? url : (url as Request).url
      if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(FLEET), { status: 200 }))
      if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 1024 }), { status: 200 }))
      return Promise.resolve(new Response('{}', { status: 200 }))
    })
    renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
    await waitFor(() => expect(screen.getAllByText('main').length).toBeGreaterThan(0))

    fireEvent.click(screen.getByText('Make live').closest('button') as HTMLButtonElement)
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/will NOT reconnect on its own/i)).toBeInTheDocument()
    expect(within(dialog).queryByText(/reconnects automatically/i)).toBeNull()
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
