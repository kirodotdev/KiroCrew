/**
 * DevFleetPage — per-pod system panel (PR C).
 *
 * Covers the three surfaces: the inline row readout, the expanded DetailPanel
 * breakdown, and the fleet-level header totals — plus the load-bearing
 * contracts: colour shifts as memory nears the cgroup ceiling, and an ABSENT
 * field renders nothing rather than a measured-looking 0.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

import DevFleetPage from '../pages/DevFleetPage'

function mockFetch(fleet: unknown, detail?: unknown) {
  vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
    const u = typeof url === 'string' ? url : (url as Request).url
    if (u.includes('/fleet')) return Promise.resolve(new Response(JSON.stringify(fleet), { status: 200 }))
    if (u.includes('/detail')) return Promise.resolve(new Response(JSON.stringify(detail ?? {}), { status: 200 }))
    if (u.includes('/disk')) return Promise.resolve(new Response(JSON.stringify({ total_mb: 51200 }), { status: 200 }))
    return Promise.resolve(new Response('{}', { status: 200 }))
  })
}

function renderPage() {
  return renderWithProviders(<DevFleetPage />, { route: '/dev-fleet' })
}

// A running pod, well under its 4 GiB ceiling.
const RUNNING_ROW = {
  name: 'feat-pods', is_main: false, running: true, has_dist: true, port: 7781, health: 200,
  behind: 0, last_updated_at: Date.now() / 1000,
  pod_resources: { mem_current: 652_242_944, mem_max: 4_294_967_296, cpu_pct: 42.5, tasks: 108, home_bytes: 3_221_225_472 },
}

describe('DevFleetPage — pod system panel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the inline readout for a running pod (memory, CPU, tasks)', async () => {
    mockFetch({ worktrees: [RUNNING_ROW] })
    renderPage()
    await waitFor(() => expect(screen.getByText('feat-pods')).toBeInTheDocument())
    // Memory rendered against its ceiling (base-1000 fmtBytes).
    expect(screen.getByTitle('Pod memory used against its cgroup ceiling')).toBeInTheDocument()
    // CPU% and task count both surface.
    expect(screen.getByTitle('Pod CPU usage')).toBeInTheDocument()
    expect(screen.getByText(/108\s+tasks/)).toBeInTheDocument()
  })

  it('shifts the memory readout to danger colour near the cgroup limit', async () => {
    const near = {
      ...RUNNING_ROW,
      pod_resources: { ...RUNNING_ROW.pod_resources, mem_current: 4_100_000_000, mem_max: 4_294_967_296 },
    }
    mockFetch({ worktrees: [near] })
    renderPage()
    await waitFor(() => expect(screen.getByText('feat-pods')).toBeInTheDocument())
    // 4.10e9 / 4.29e9 ≈ 0.955 >= 0.9 -> danger. jsdom does not resolve CSS
    // custom properties through getComputedStyle, so assert the inline style
    // attribute carries the danger token directly.
    const mem = screen.getByTitle('Pod memory used against its cgroup ceiling')
    expect(mem.getAttribute('style')).toContain('var(--danger)')
  })

  it('renders NOTHING for a running pod whose resources are absent', async () => {
    // Off Linux / probe failed: pod_resources is null. No readout, no fake 0.
    const absent = { ...RUNNING_ROW, pod_resources: null }
    mockFetch({ worktrees: [absent] })
    renderPage()
    await waitFor(() => expect(screen.getByText('feat-pods')).toBeInTheDocument())
    expect(screen.queryByTitle('Pod memory used against its cgroup ceiling')).toBeNull()
    expect(screen.queryByTitle('Pod CPU usage')).toBeNull()
    expect(screen.queryByText(/\btasks\b/)).toBeNull()
  })

  it('renders only the present fields (CPU absent on first sample renders no CPU)', async () => {
    const partial = {
      ...RUNNING_ROW,
      // First CPU sample -> cpu_pct null; memory present. Readout shows memory,
      // omits CPU entirely rather than "0%".
      pod_resources: { mem_current: 500_000_000, mem_max: 4_294_967_296, cpu_pct: null, tasks: 12, home_bytes: null },
    }
    mockFetch({ worktrees: [partial] })
    renderPage()
    await waitFor(() => expect(screen.getByText('feat-pods')).toBeInTheDocument())
    expect(screen.getByTitle('Pod memory used against its cgroup ceiling')).toBeInTheDocument()
    expect(screen.queryByTitle('Pod CPU usage')).toBeNull()
  })

  it('shows the full breakdown incl. pod HOME size in the expanded DetailPanel', async () => {
    mockFetch(
      { worktrees: [RUNNING_ROW] },
      { name: 'feat-pods', branch: 'feat/pods', pod_running: true, pod_port: 7781 },
    )
    renderPage()
    await waitFor(() => expect(screen.getByText('feat-pods')).toBeInTheDocument())
    // Expand the row.
    fireEvent.click(screen.getByLabelText('Expand'))
    await waitFor(() => expect(screen.getByText(/Pod home:/)).toBeInTheDocument())
    // HOME size formatted (3.22 GB, base-1000).
    expect(screen.getByText(/Pod home:\s*3\.2/)).toBeInTheDocument()
    expect(screen.getByText(/Tasks:\s*108/)).toBeInTheDocument()
  })

  it('renders the fleet-level totals strip in the header', async () => {
    mockFetch({
      worktrees: [RUNNING_ROW],
      fleet_totals: { pod_home_bytes: 3_221_225_472, orphan_pods: 2 },
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('fleet-totals')).toBeInTheDocument())
    const totals = screen.getByTestId('fleet-totals')
    expect(within(totals).getByText(/Pod-home disk:/)).toBeInTheDocument()
    expect(within(totals).getByText(/Orphan pod homes:\s*2/)).toBeInTheDocument()
    // Worktree disk is owned by the pre-existing `/disk`-backed stat card, so
    // the strip must NOT carry a second figure for it -- one label with two
    // independently-measured numbers is a number nobody can act on.
    expect(within(totals).queryByText(/Worktree disk:/)).toBeNull()
  })

  it('hides the totals strip when no total is measurable', async () => {
    mockFetch({
      worktrees: [{ ...RUNNING_ROW, pod_resources: null }],
      fleet_totals: { pod_home_bytes: null, orphan_pods: 0 },
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('feat-pods')).toBeInTheDocument())
    expect(screen.queryByTestId('fleet-totals')).toBeNull()
  })
})
