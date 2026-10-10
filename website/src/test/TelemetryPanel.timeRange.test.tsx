/**
 * Telemetry panel: one window control drives every card.
 *
 * The properties pinned here are the ones that fail quietly: a pick that never
 * reaches the request (the cards keep showing the old week), a cached week served
 * as a month (the query key ignoring the range), a card title that keeps saying
 * "Last 7d" over a custom range, and a control that disappears exactly when the
 * reader picked a window with nothing in it and needs to widen it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'
import { localDay } from '../pages/telemetryRange'
import { fmtDate } from '../i18n/format'

const cost = (over: Record<string, unknown> = {}) => ({
  window_days: 7,
  credits: 1000,
  turns: 100,
  per_turn: 10,
  prior_credits: 500,
  prior_turns: 50,
  prior_per_turn: 10,
  delta_pct: 100,
  priciest: { credits: 90, slot: 'chat-7-1700000700', ts: '2026-08-05' },
  by_model: [],
  by_channel: [],
  by_category: [],
  context_bands: [],
  conversations: [],
  conversation_count: 0,
  navigable_category: 'dashboard',
  ...over,
})

const payload = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  window_days: 7,
  window_start: '2026-09-24T12:00:00Z',
  window_end: '2026-10-01T12:00:00Z',
  window_rolling: true,
  metrics_retention_days: 0,
  shard_count: 1,
  metrics_dir: '/tmp/metrics',
  startup: null,
  turn: null,
  context: null,
  cost: cost(),
  other: [],
  ...over,
})

vi.mock('../api/client', () => ({ api: { telemetryStartup: vi.fn() } }))

const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
)

async function mount(over: Record<string, unknown> = {}) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue(payload(over) as never)
  render(<TelemetryPanel />, { wrapper: Wrapper })
  return api
}

const control = () => within(screen.getByTestId('telemetry-range'))

// One local instant for the panel's clock and the tests' own expected dates, so a
// midnight between render and assertion cannot move one side a day. Only Date is
// faked: timers stay real for waitFor and user-event.
const FROZEN = new Date(2026, 5, 15, 12, 0, 0)

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.useFakeTimers({ toFake: ['Date'], now: FROZEN })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('TelemetryPanel — the window control', () => {
  it('opens on Default: no window asked for, and each card keeps its own main window', async () => {
    const turn = { count: 80, mean_ms: 100, p50_ms: 90, p90_ms: 200, min_ms: 10, max_ms: 300, other_generations: 0, total_count: 80, outcome: { ok: 80 }, fault_rate: 0 }
    const api = await mount({ window_days: 14, window_default: true, cost_window_days: 7, turn })
    // The spend card says its week; the health strip (OTEL) says its fortnight.
    expect(await screen.findAllByText('Last 7d')).not.toHaveLength(0)
    expect(screen.getAllByText('Last 14d')).not.toHaveLength(0)
    expect(vi.mocked(api.telemetryStartup)).toHaveBeenCalledWith('')
    expect(control().getByRole('radio', { name: 'Default' })).toHaveAttribute('aria-checked', 'true')
    // Nothing picked, so nothing but Default can be remembered.
    expect(localStorage.getItem('telemetry:range') ?? 'default').toBe('default')
  })

  it('asks for the picked week and labels every card with it', async () => {
    localStorage.setItem('telemetry:range', '7d')
    const api = await mount()
    expect(await screen.findAllByText('Last 7d')).not.toHaveLength(0)
    expect(vi.mocked(api.telemetryStartup)).toHaveBeenCalledWith('days=7')
    expect(control().getByRole('radio', { name: '7d' })).toHaveAttribute('aria-checked', 'true')
  })

  it('refetches with the picked preset and remembers it', async () => {
    const user = userEvent.setup()
    const api = await mount()
    await screen.findAllByText('Last 7d')
    vi.mocked(api.telemetryStartup).mockResolvedValue(payload({ window_days: 30 }) as never)
    await user.click(control().getByRole('radio', { name: '30d' }))
    await waitFor(() => expect(vi.mocked(api.telemetryStartup)).toHaveBeenLastCalledWith('days=30'))
    expect(await screen.findAllByText('Last 30d')).not.toHaveLength(0)
    expect(localStorage.getItem('telemetry:range')).toBe('30d')
  })

  it('names the 24-hour preset in hours', async () => {
    localStorage.setItem('telemetry:range', '24h')
    const api = await mount({ window_days: 1 })
    expect(await screen.findAllByText('Last 24h')).not.toHaveLength(0)
    expect(vi.mocked(api.telemetryStartup)).toHaveBeenCalledWith('days=1')
  })

  it('sends a custom range as epoch bounds and titles the cards with its dates', async () => {
    // Relative to today: a fixed date would fall behind the 90-day ceiling
    // and be moved up to the earliest legal day.
    const t = new Date()
    const start = new Date(t.getFullYear(), t.getMonth(), t.getDate() - 10)
    const endExclusive = new Date(t.getFullYear(), t.getMonth(), t.getDate() - 7)
    const lastDay = new Date(t.getFullYear(), t.getMonth(), t.getDate() - 8)
    localStorage.setItem('telemetry:range', 'custom')
    localStorage.setItem('telemetry:range-since', localDay(start))
    localStorage.setItem('telemetry:range-until', localDay(lastDay))
    const api = await mount({
      window_days: 3,
      window_rolling: false,
      window_start: start.toISOString(),
      window_end: endExclusive.toISOString(),
    })
    const label = `${fmtDate(start)} – ${fmtDate(new Date(endExclusive.getTime() - 1))}`
    expect(await screen.findAllByText(label)).not.toHaveLength(0)
    expect(vi.mocked(api.telemetryStartup)).toHaveBeenCalledWith(
      `since=${start.getTime() / 1000}&until=${endExclusive.getTime() / 1000}`,
    )
    expect(control().getByLabelText('Start date')).toHaveValue(localDay(start))
    expect(control().getByLabelText('End date')).toHaveValue(localDay(lastDay))
  })

  it('shows the date pickers only for the custom choice', async () => {
    const user = userEvent.setup()
    await mount()
    await screen.findAllByText('Last 7d')
    expect(control().queryByLabelText('Start date')).toBeNull()
    await user.click(control().getByRole('radio', { name: 'Custom' }))
    expect(control().getByLabelText('Start date')).toBeInTheDocument()
    expect(control().getByLabelText('End date')).toBeInTheDocument()
  })

  it('keeps the control on screen when the window holds no data', async () => {
    await mount({ cost: null, context: null })
    expect(await screen.findByText('No telemetry recorded in this period (Last 7d).')).toBeInTheDocument()
    expect(screen.getByTestId('telemetry-range')).toBeInTheDocument()
  })

  const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString()

  it('says when metric retention is shorter than the window', async () => {
    await mount({ window_days: 30, window_start: daysAgo(30), metrics_retention_days: 14 })
    expect(await screen.findByTestId('telemetry-otel-retention')).toHaveTextContent('cover only the last 14d')
  })

  it('says why in user-level words: older metrics are deleted', async () => {
    await mount({ window_days: 30, window_start: daysAgo(30), metrics_retention_days: 14 })
    const line = await screen.findByTestId('telemetry-otel-retention')
    expect(line).toHaveTextContent('older metrics are deleted (')
    // The day count is said once, not again after "deleted".
    expect(line.textContent?.match(/14d/g)).toHaveLength(1)
    expect(line).not.toHaveTextContent('prunes')
  })

  it('says the figures are still the old pick while the new window loads', async () => {
    const user = userEvent.setup()
    const api = await mount()
    await screen.findAllByText('Last 7d')
    expect(screen.getByTestId('telemetry-range-pending')).toHaveTextContent('')
    let land: (v: unknown) => void = () => {}
    vi.mocked(api.telemetryStartup).mockReturnValue(new Promise(r => (land = r)) as never)
    await user.click(control().getByRole('radio', { name: '90d' }))
    await waitFor(() => expect(screen.getByTestId('telemetry-range-pending')).toHaveTextContent('Loading telemetry'))
    land(payload({ window_days: 90 }))
    expect(await screen.findAllByText('Last 90d')).not.toHaveLength(0)
    expect(screen.getByTestId('telemetry-range-pending')).toHaveTextContent('')
  })

  it('keeps the loading line in its own reserved row, so the cards do not move on a pick', async () => {
    // At 390px an inline status wrapped onto a row of its own only while a pick
    // was loading, pushing every card down and back. happy-dom computes no
    // layout, so pin the contract instead: one element, always rendered, a full
    // row of fixed height, the same before, during and after the load.
    const user = userEvent.setup()
    const api = await mount()
    await screen.findAllByText('Last 7d')
    const before = screen.getByTestId('telemetry-range-pending')
    const reserved = ['basis-full', 'h-4']
    for (const cls of reserved) expect(before.className.split(/\s+/)).toContain(cls)
    let land: (v: unknown) => void = () => {}
    vi.mocked(api.telemetryStartup).mockReturnValue(new Promise(r => (land = r)) as never)
    await user.click(control().getByRole('radio', { name: '90d' }))
    await waitFor(() => expect(screen.getByTestId('telemetry-range-pending')).toHaveTextContent('Loading telemetry'))
    const during = screen.getByTestId('telemetry-range-pending')
    expect(during).toBe(before)
    expect(during.className).toBe(before.className)
    land(payload({ window_days: 90 }))
    expect(await screen.findAllByText('Last 90d')).not.toHaveLength(0)
    expect(screen.getByTestId('telemetry-range-pending')).toBe(before)
    expect(screen.getByTestId('telemetry-range-pending').className).toBe(during.className)
  })

  it('says it for a short custom range that starts before retention', async () => {
    await mount({ window_days: 3, window_rolling: false, window_start: daysAgo(31), window_end: daysAgo(28), metrics_retention_days: 14 })
    expect(await screen.findByTestId('telemetry-otel-retention')).toHaveTextContent('cover only the last 14d')
  })

  it('stays quiet when retention covers the window', async () => {
    await mount({ window_days: 7, window_start: daysAgo(7), metrics_retention_days: 14 })
    await screen.findAllByText('Last 7d')
    expect(screen.queryByTestId('telemetry-otel-retention')).toBeNull()
  })

  it('names the retention setting as a setting reference, not raw text', async () => {
    await mount({ window_days: 30, window_start: daysAgo(30), metrics_retention_days: 14 })
    const line = await screen.findByTestId('telemetry-otel-retention')
    // Its own element, as `off_body` renders `telemetry.enabled`.
    expect(within(line).getByText('telemetry.retention_days')).toBeInTheDocument()
  })

  it('keeps the retention setting chip clear of the brackets around it', async () => {
    await mount({ window_days: 30, window_start: daysAgo(30), metrics_retention_days: 14 })
    const line = await screen.findByTestId('telemetry-otel-retention')
    const chip = within(line).getByTestId('retention-setting-ref')
    expect(chip.className.split(/\s+/)).toEqual(expect.arrayContaining(['mx-1', 'inline-block']))
    expect(within(chip).getByText('telemetry.retention_days')).toBeInTheDocument()
  })

  it('titles a retention-capped OTEL card with the span it covers', async () => {
    const turn = { count: 80, mean_ms: 100, p50_ms: 90, p90_ms: 200, min_ms: 10, max_ms: 300, other_generations: 0, total_count: 80, outcome: { ok: 80 }, fault_rate: 0 }
    await mount({ window_days: 30, window_start: daysAgo(30), metrics_retention_days: 14, turn })
    // The health strip's throughput holds 14 days of shards, not the 30 picked,
    // while the spend card (per-turn rows, never pruned) keeps the panel's 30.
    expect(await screen.findAllByText('Last 14d')).not.toHaveLength(0)
    expect(screen.getAllByText('Last 30d')).not.toHaveLength(0)
  })

  it('names the period the OTEL sections cover in the footer, not the pick', async () => {
    await mount({ window_days: 30, window_start: daysAgo(30), metrics_retention_days: 14 })
    await screen.findByTestId('telemetry-otel-retention')
    // The footer speaks for the OTEL sections, which hold 14 days of shards.
    expect(screen.getByText(/OTEL sections · period: Last 14d ·/)).toBeInTheDocument()
    expect(screen.queryByText(/OTEL sections · period: Last 30d/)).toBeNull()
  })

  it('offers no custom start day that would span more than 90 dates', async () => {
    localStorage.setItem('telemetry:range', 'custom')
    await mount()
    await screen.findByTestId('telemetry-range')
    const now = new Date()
    const earliest = localDay(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 89))
    expect(control().getByLabelText('Start date')).toHaveAttribute('min', earliest)
  })

  it('sizes the period segments to their labels so the control fits one desktop row', async () => {
    await mount()
    await screen.findByTestId('telemetry-range')
    const group = control().getByRole('radiogroup')
    expect(group.className.split(/\s+/)).not.toContain('w-full')
    for (const radio of control().getAllByRole('radio')) expect(radio.className.split(/\s+/)).not.toContain('flex-1')
  })

  it('clears only the date being edited and keeps the other one', async () => {
    // Keyboard editing a date input passes through '' between keystrokes; that
    // must reset the field being typed in, never the other end of the range.
    const t = new Date()
    const day = (n: number) => localDay(new Date(t.getFullYear(), t.getMonth(), t.getDate() - n))
    localStorage.setItem('telemetry:range', 'custom')
    localStorage.setItem('telemetry:range-since', day(20))
    localStorage.setItem('telemetry:range-until', day(18))
    await mount()
    await screen.findByTestId('telemetry-range')
    expect(control().getByLabelText('Start date')).toHaveValue(day(20))
    // A blank start falls back to the week ending on the kept end day.
    fireEvent.change(control().getByLabelText('Start date'), { target: { value: '' } })
    expect(control().getByLabelText('End date')).toHaveValue(day(18))
    expect(control().getByLabelText('Start date')).toHaveValue(day(24))
    // A blank end falls back to today and keeps the start.
    fireEvent.change(control().getByLabelText('Start date'), { target: { value: day(20) } })
    fireEvent.change(control().getByLabelText('End date'), { target: { value: '' } })
    expect(control().getByLabelText('Start date')).toHaveValue(day(20))
    expect(control().getByLabelText('End date')).toHaveValue(day(0))
  })
})
