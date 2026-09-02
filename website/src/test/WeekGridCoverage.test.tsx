import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import WeekGrid, { parseCronSlots } from '../components/WeekGrid'
import { renderWithProviders } from './helpers'
import type { CronJob } from '../types'

/** Tuesday 17:00 UTC — pinned so weekday, week dates and the now-line are
 *  deterministic regardless of the host clock or DST. */
const NOW = new Date('2026-05-12T17:00:00Z')
/** 17:20 UTC on the same day, used as an interval-job anchor timestamp. */
const ANCHOR_TS = Math.floor(new Date('2026-05-12T17:20:00Z').getTime() / 1000)

const job = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'j1',
  name: 'nightly report',
  message: 'run it',
  enabled: true,
  schedule: '0 9 * * 1',
  last_status: 'ok',
  cron_expr: '0 9 * * 1',
  timezone: 'UTC',
  ...overrides,
})

describe('parseCronSlots — interval schedules', () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: NOW })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('parses a sub-hour interval out of the schedule string and emits one slot per hour cell', () => {
    const slots = parseCronSlots(
      job({ cron_expr: null, every_secs: null, schedule: 'every 900s' }),
      'UTC',
    )
    // 24 hours x 7 days: a 15-minute job fires inside every hour cell.
    expect(slots).toHaveLength(168)
    expect(new Set(slots.map(s => s.minute))).toEqual(new Set([0]))
  })

  it('reads the hour suffix form ("every 2h") and emits each fire time', () => {
    const slots = parseCronSlots(
      job({ cron_expr: null, every_secs: null, schedule: 'every 2h' }),
      'UTC',
    )
    expect(slots).toHaveLength(84) // 12 fires x 7 days
    const hours = [...new Set(slots.map(s => s.hour))].sort((a, b) => a - b)
    expect(hours).toEqual([0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22])
  })

  it('anchors an hourly interval on the job creation timestamp', () => {
    const slots = parseCronSlots(
      job({ cron_expr: null, every_secs: 3600, created_ts: ANCHOR_TS }),
      'UTC',
    )
    expect(slots).toHaveLength(168) // 24 fires x 7 days
    const hours = [...new Set(slots.map(s => s.hour))].sort((a, b) => a - b)
    expect(hours).toHaveLength(24)
    // Every fire sits at the same offset inside its hour.
    expect(new Set(slots.map(s => s.minute)).size).toBe(1)
  })

  it('anchors a daily-or-longer interval on the last run timestamp', () => {
    const slots = parseCronSlots(
      job({ cron_expr: null, every_secs: 86_400, last_run_ts: ANCHOR_TS }),
      'UTC',
    )
    expect(slots).toHaveLength(7)
    expect(new Set(slots.map(s => s.hour))).toEqual(new Set([17]))
    expect(new Set(slots.map(s => s.minute))).toEqual(new Set([20]))
  })

  it('ignores a zero interval and falls through to the cron expression', () => {
    const slots = parseCronSlots(job({ every_secs: 0, cron_expr: '0 9 * * 1' }), 'UTC')
    expect(slots).toHaveLength(1)
    expect(slots[0].hour).toBe(9)
  })
})

describe('parseCronSlots — cron expression fields', () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: NOW })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('returns no slots for an expression that is not five fields', () => {
    expect(parseCronSlots(job({ cron_expr: '0 9 * *' }), 'UTC')).toEqual([])
  })

  it('treats day-of-week 7 as Sunday', () => {
    const slots = parseCronSlots(job({ cron_expr: '0 12 * * 7' }), 'UTC')
    expect(slots).toHaveLength(1)
    expect(slots[0].day).toBe(6) // grid is Monday-first, so Sunday is index 6
  })

  it('de-duplicates day-of-week 0 and 7 into a single Sunday column', () => {
    const slots = parseCronSlots(job({ cron_expr: '0 12 * * 0,7' }), 'UTC')
    expect(slots).toHaveLength(1)
    expect(slots[0].day).toBe(6)
  })

  it('expands comma lists and ranges', () => {
    const slots = parseCronSlots(job({ cron_expr: '0,30 8-9 * * 1,3' }), 'UTC')
    expect(slots).toHaveLength(8) // 2 minutes x 2 hours x 2 days
    expect([...new Set(slots.map(s => s.hour))].sort((a, b) => a - b)).toEqual([8, 9])
    expect([...new Set(slots.map(s => s.minute))].sort((a, b) => a - b)).toEqual([0, 30])
  })

  it('applies a step to an explicit range', () => {
    const slots = parseCronSlots(job({ cron_expr: '0 8-14/3 * * 1' }), 'UTC')
    expect([...new Set(slots.map(s => s.hour))].sort((a, b) => a - b)).toEqual([8, 11, 14])
  })

  it('applies a step to a wildcard field', () => {
    const slots = parseCronSlots(job({ cron_expr: '0 */6 * * 1' }), 'UTC')
    expect([...new Set(slots.map(s => s.hour))].sort((a, b) => a - b)).toEqual([0, 6, 12, 18])
  })

  it('drops unparseable and out-of-range field values', () => {
    const slots = parseCronSlots(job({ cron_expr: 'x,99,15 9 * * 1' }), 'UTC')
    expect(slots).toHaveLength(1)
    expect(slots[0].minute).toBe(15)
  })

  it('yields nothing when every value in a field is out of range', () => {
    expect(parseCronSlots(job({ cron_expr: '99 9 * * 1' }), 'UTC')).toEqual([])
  })
})

describe('WeekGrid rendering', () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: NOW })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders a Monday-first header with the current week dates in the render TZ', () => {
    renderWithProviders(<WeekGrid jobs={[]} onSelect={vi.fn()} renderTz="UTC" />)

    expect(screen.getByText('Mon')).toBeInTheDocument()
    expect(screen.getByText('Sun')).toBeInTheDocument()
    // Week of Monday 2026-05-11 through Sunday 2026-05-17.
    expect(screen.getByText('05/11')).toBeInTheDocument()
    expect(screen.getByText('05/17')).toBeInTheDocument()
  })

  it('marks the current day column as today', () => {
    renderWithProviders(<WeekGrid jobs={[]} onSelect={vi.fn()} renderTz="UTC" />)

    // NOW is a Tuesday, so only the Tue column is accented.
    expect(screen.getByText('Tue').parentElement?.className).toContain('text-accent')
    expect(screen.getByText('Wed').parentElement?.className).toContain('text-muted')
  })

  it('renders all 24 hour rows', () => {
    renderWithProviders(<WeekGrid jobs={[]} onSelect={vi.fn()} renderTz="UTC" />)

    expect(screen.getAllByText(/^([01]\d|2[0-3]):00$/)).toHaveLength(24)
  })

  it('renders no dots and no legend when there are no jobs', () => {
    renderWithProviders(<WeekGrid jobs={[]} onSelect={vi.fn()} renderTz="UTC" />)

    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })

  it('falls back to the resolved local timezone when no render TZ is given', () => {
    renderWithProviders(<WeekGrid jobs={[job()]} onSelect={vi.fn()} />)

    expect(screen.getAllByText(/^([01]\d|2[0-3]):00$/)).toHaveLength(24)
    // The job still lands somewhere on the grid, whatever the host zone is.
    expect(screen.getAllByRole('button', { name: /nightly report at/ })).toHaveLength(1)
  })

  it('places one dot per slot, labelled with the time in the render TZ', () => {
    renderWithProviders(<WeekGrid jobs={[job()]} onSelect={vi.fn()} renderTz="UTC" />)

    const dot = screen.getByRole('button', { name: 'nightly report at 09:00 UTC' })
    expect(dot).toHaveAttribute('title', 'nightly report — 09:00 UTC')
  })

  it('calls onSelect when a dot is clicked', () => {
    const onSelect = vi.fn()
    const j = job()
    renderWithProviders(<WeekGrid jobs={[j]} onSelect={onSelect} renderTz="UTC" />)

    fireEvent.click(screen.getByRole('button', { name: 'nightly report at 09:00 UTC' }))
    expect(onSelect).toHaveBeenCalledWith(j)
  })

  it('rings the dots of the selected job only', () => {
    const jobs = [job(), job({ id: 'j2', name: 'weekly digest', cron_expr: '0 10 * * 1' })]
    renderWithProviders(<WeekGrid jobs={jobs} selectedId="j2" onSelect={vi.fn()} renderTz="UTC" />)

    expect(screen.getByRole('button', { name: 'weekly digest at 10:00 UTC' }).className)
      .toContain('ring-2')
    expect(screen.getByRole('button', { name: 'nightly report at 09:00 UTC' }).className)
      .not.toContain('ring-2')
  })

  it('dims a paused job and says so in the dot tooltip', () => {
    renderWithProviders(
      <WeekGrid jobs={[job({ enabled: false })]} onSelect={vi.fn()} renderTz="UTC" />,
    )

    const dot = screen.getByRole('button', { name: 'nightly report at 09:00 UTC' })
    expect(dot).toHaveAttribute('title', 'nightly report (paused) — 09:00 UTC')
    expect(dot.className).toContain('opacity-30')
  })

  it('renders a legend entry per job and selects from it', () => {
    const onSelect = vi.fn()
    const jobs = [job(), job({ id: 'j2', name: 'weekly digest', cron_expr: '0 10 * * 1' })]
    renderWithProviders(
      <WeekGrid jobs={jobs} selectedId="j1" onSelect={onSelect} renderTz="UTC" />,
    )

    const legendEntry = screen.getByRole('button', { name: 'weekly digest' })
    expect(screen.getByRole('button', { name: 'nightly report' }).className).toContain('font-medium')
    expect(legendEntry.className).toContain('text-muted')

    fireEvent.click(legendEntry)
    expect(onSelect).toHaveBeenCalledWith(jobs[1])
  })

  it('marks a paused job as paused in the legend', () => {
    renderWithProviders(
      <WeekGrid jobs={[job({ enabled: false })]} onSelect={vi.fn()} renderTz="UTC" />,
    )

    expect(screen.getByRole('button', { name: 'nightly report (paused)' })).toBeInTheDocument()
  })

  it('gives each job its own colour, cycling once the palette runs out', () => {
    const jobs = Array.from({ length: 9 }, (_, i) =>
      job({ id: `j${i}`, name: `job-${i}`, cron_expr: `0 ${i} * * 1` }),
    )
    renderWithProviders(<WeekGrid jobs={jobs} onSelect={vi.fn()} renderTz="UTC" />)

    const first = screen.getByRole('button', { name: /^job-0 at/ }).className
    const ninth = screen.getByRole('button', { name: /^job-8 at/ }).className
    // Palette has 8 entries, so the ninth job reuses the first colour.
    expect(ninth.split(' ').filter(c => c.startsWith('bg-')))
      .toEqual(first.split(' ').filter(c => c.startsWith('bg-')))
  })

  it('advances the now-line as the clock ticks', () => {
    const { container } = renderWithProviders(
      <WeekGrid jobs={[]} onSelect={vi.fn()} renderTz="UTC" />,
    )

    const line = () => container.querySelector<HTMLElement>('.pointer-events-none.z-10')
    // 17:00 exactly → the line sits at the top of the 17:00 row.
    expect(line()?.style.top).toBe('0%')

    act(() => {
      vi.advanceTimersByTime(30 * 60_000)
    })
    // 17:30 → halfway down the same row.
    expect(line()?.style.top).toBe('50%')
  })

  it('re-reads the clock when the render timezone changes', () => {
    const { container, rerender } = renderWithProviders(
      <WeekGrid jobs={[]} onSelect={vi.fn()} renderTz="UTC" />,
    )
    const rows = () => container.querySelectorAll('.pointer-events-none.z-10').length

    expect(rows()).toBe(7) // one segment per day column, on the current hour row
    rerender(<WeekGrid jobs={[]} onSelect={vi.fn()} renderTz="Asia/Kolkata" />)
    expect(rows()).toBe(7)
    // 17:00 UTC is 22:30 in Asia/Kolkata, so the line moved to the 22:00 row.
    expect(container.querySelector<HTMLElement>('.pointer-events-none.z-10')?.style.top)
      .toBe('50%')
  })
})
