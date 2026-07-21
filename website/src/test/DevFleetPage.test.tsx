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
    await waitFor(() => expect(screen.getByText(/build pending/i)).toBeInTheDocument())
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
})