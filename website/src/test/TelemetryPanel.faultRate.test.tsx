/**
 * Telemetry panel: the fault-rate tile must not report a real failure as zero.
 *
 * Measured against the real row store, the turn histogram carried
 * `{ok: 498, error: 1}` — `fault_rate` 0.002. The tile rendered that as a bold
 * `0%` in `--ok` green, directly above a sub line reading "1 faults / 499
 * turns". Two independent defects produced it:
 *
 *  1. `Math.round(0.002 * 100)` is 0, so every fault rate under 0.5% collapsed
 *     to a clean zero — the same rounding lie that made a 0.4% spend share read
 *     as `0%` in the credits tables.
 *  2. The colour branch was `faultPct < 2 → --ok`, keyed on the ROUNDED
 *     percentage rather than on whether any fault exists, so a window with real
 *     failures was painted in the success colour.
 *
 * A third defect was latent rather than visible: the fault COUNT summed only
 * `error` + `timeout`, while the API derives `fault_rate` from every outcome
 * that is not `ok`. Any other value — including the `unknown` that shards
 * predating the outcome attribute aggregate under — was excluded from the
 * count but included in the rate, so the tile showed a rate over one
 * population beside a count over another.
 *
 * These are display-layer defects only. Both underlying instruments are
 * fail-closed and were measured to be honest: `_turn_outcome` maps a real
 * `stop_reason`, and both startup emit paths default to `error` and overwrite
 * with `ready` only on the success path — so the 100% ready rate is a genuine
 * 1411/1411, not a tautology.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import TelemetryPanel from '../pages/TelemetryPanel'

const stat = (over: Record<string, number> = {}) => ({
  count: 10, mean_ms: 100, p50_ms: 90, p90_ms: 200, min_ms: 10, max_ms: 300,
  other_generations: 0, total_count: 10, ...over,
})

const resp = (turn: Record<string, unknown>) => ({
  enabled: true,
  window_days: 14,
  shard_count: 3,
  metrics_dir: '/tmp/metrics',
  startup: {
    overall: stat(), cold: stat(), warm: stat(),
    outcome: { ready: 10 },
    daily: [],
    distribution: { buckets: [0, 7, 3], bounds: [3000, 5000] },
    phases: [],
  },
  turn,
  context: null,
  other: [],
})

vi.mock('../api/client', () => ({
  api: { telemetryStartup: vi.fn() },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

async function mount(payload: Record<string, unknown>) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue(payload as never)
  render(<TelemetryPanel />, { wrapper: Wrapper })
}

/** The whole tile card, so value and sub line can be asserted together. */
const faultTile = () => screen.getByText('Fault rate').parentElement as HTMLElement
const faultValue = () => faultTile().querySelector('.font-bold') as HTMLElement

describe('TelemetryPanel fault rate', () => {
  beforeEach(() => { vi.clearAllMocks(); qc.clear() })

  it('shows a sub-threshold fault rate as "<1", never as a bare 0', async () => {
    // The exact shape measured on the real store.
    await mount(resp({ ...stat({ count: 499 }), outcome: { ok: 498, error: 1 }, fault_rate: 0.002 }))
    await waitFor(() => expect(screen.getByText('Fault rate')).toBeInTheDocument())

    expect(faultValue().textContent).toBe('<1%')
    // The rounded zero is what the tile used to claim. Assert its absence
    // explicitly: a regression here is silent, because "0%" looks plausible.
    expect(faultValue().textContent).not.toBe('0%')
  })

  it('does not paint the success colour while faults exist', async () => {
    await mount(resp({ ...stat({ count: 499 }), outcome: { ok: 498, error: 1 }, fault_rate: 0.002 }))
    await waitFor(() => expect(screen.getByText('Fault rate')).toBeInTheDocument())

    // --ok is reserved for an empty fault set; one real error is not one.
    expect(faultValue().style.color).not.toBe('var(--ok)')
  })

  it('keeps the success colour and a plain 0 when nothing failed', async () => {
    await mount(resp({ ...stat({ count: 80 }), outcome: { ok: 80 }, fault_rate: 0 }))
    await waitFor(() => expect(screen.getByText('Fault rate')).toBeInTheDocument())

    expect(faultValue().textContent).toBe('0%')
    expect(faultValue().style.color).toBe('var(--ok)')
  })

  it('counts every non-ok outcome, so the count matches the rate the API sent', async () => {
    // 2 of 100 are faults, but only ONE of them is named `error`. The old
    // `error + timeout` sum reported 1 beside a rate derived from 2.
    await mount(resp({
      ...stat({ count: 100 }),
      outcome: { ok: 98, error: 1, unknown: 1 },
      fault_rate: 0.02,
    }))
    await waitFor(() => expect(screen.getByText('Fault rate')).toBeInTheDocument())

    expect(faultValue().textContent).toBe('2%')
    expect(faultTile().textContent).toContain('2 faults of 100')
  })

  it('says "1 fault", not "1 faults", when exactly one turn failed', async () => {
    await mount(resp({ ...stat({ count: 499 }), outcome: { ok: 498, error: 1 }, fault_rate: 0.002 }))
    await waitFor(() => expect(screen.getByText('Fault rate')).toBeInTheDocument())

    expect(faultTile().textContent).toContain('1 fault of 499')
    expect(faultTile().textContent).not.toContain('1 faults')
  })

  it('renders an em dash rather than a zero when no turns were recorded', async () => {
    await mount({
      enabled: true, window_days: 14, shard_count: 1, metrics_dir: '/tmp/m',
      startup: {
        overall: stat(), cold: stat(), warm: stat(), outcome: { ready: 10 },
        daily: [], distribution: { buckets: [10], bounds: [] }, phases: [],
      },
      turn: null, context: null, other: [],
    })
    await waitFor(() => expect(screen.getByText('Fault rate')).toBeInTheDocument())

    expect(faultValue().textContent).toBe('—')
  })
})

/**
 * The startup tile reports a COUNT, because a rate could not report a failure.
 *
 * The window measured 1411 startups, all `ready`. One failure in that
 * population is 99.93%, which `Math.round` takes back to a perfect `100%` — so
 * the old "Ready rate" had no reachable value between "100%" and a problem
 * large enough to survive rounding. It saturated, and it saturated in the
 * direction that hides bad news. A count cannot: the first real failure moves
 * the headline from 0 to 1.
 *
 * Both startup emit paths are fail-closed (`_startup_outcome` / `outcome`
 * default to `error` and are overwritten with `ready` only on the success
 * path), so a zero here is a measurement, not an instrument that cannot fail.
 */
const startupTile = () => screen.getByText('Startup faults').parentElement as HTMLElement
const startupValue = () => startupTile().querySelector('.font-bold') as HTMLElement

describe('TelemetryPanel startup faults', () => {
  beforeEach(() => { vi.clearAllMocks(); qc.clear() })

  const withStartupOutcome = (outcome: Record<string, number>) => ({
    enabled: true, window_days: 14, shard_count: 1, metrics_dir: '/tmp/m',
    startup: {
      overall: stat({ count: Object.values(outcome).reduce((a, b) => a + b, 0) }),
      cold: stat(), warm: stat(), outcome,
      daily: [], distribution: { buckets: [10], bounds: [] }, phases: [],
    },
    turn: { ...stat({ count: 80 }), outcome: { ok: 80 }, fault_rate: 0 },
    context: null, other: [],
  })

  it('shows the fault count and the population it was measured over', async () => {
    await mount(withStartupOutcome({ ready: 1411 }))
    await waitFor(() => expect(screen.getByText('Startup faults')).toBeInTheDocument())

    expect(startupValue().textContent).toBe('0')
    expect(startupTile().textContent).toContain('1,411 startups recorded')
    expect(startupValue().style.color).toBe('var(--ok)')
  })

  it('surfaces a single failure that a rate would have rounded to 100%', async () => {
    // 1410/1411 ready is 99.93%. The retired tile rendered that as "100%".
    await mount(withStartupOutcome({ ready: 1410, error: 1 }))
    await waitFor(() => expect(screen.getByText('Startup faults')).toBeInTheDocument())

    expect(startupValue().textContent).toBe('1')
    expect(startupValue().style.color).toBe('var(--danger)')
  })

  it('counts every non-ready outcome, including auth_required', async () => {
    // A not-logged-in exit is a startup that never became ready. Naming only
    // `error` would drop it and report a clean zero.
    await mount(withStartupOutcome({ ready: 20, error: 1, auth_required: 2 }))
    await waitFor(() => expect(screen.getByText('Startup faults')).toBeInTheDocument())

    expect(startupValue().textContent).toBe('3')
    expect(startupTile().textContent).toContain('23 startups recorded')
  })

  it('uses the singular when exactly one startup was recorded', async () => {
    await mount(withStartupOutcome({ ready: 1 }))
    await waitFor(() => expect(screen.getByText('Startup faults')).toBeInTheDocument())

    expect(startupTile().textContent).toContain('1 startup recorded')
    expect(startupTile().textContent).not.toContain('1 startups')
  })

  it('no longer offers a ready rate', async () => {
    await mount(withStartupOutcome({ ready: 1411 }))
    await waitFor(() => expect(screen.getByText('Startup faults')).toBeInTheDocument())

    // The label is gone from the catalog too, so this also guards against the
    // tile being reinstated from a stale key.
    expect(screen.queryByText('Ready rate')).not.toBeInTheDocument()
  })
})
