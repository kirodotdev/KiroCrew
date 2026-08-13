import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import {
  TH_CLS, TD_CLS, renderThCells, fmtSchedule, SaveCreateLabel, expandDow, fmtCron,
} from '../utils/cronUtils'
import type { CronJob } from '../types'

/**
 * Coverage for the parts of cronUtils the existing suite skips: the schedule
 * formatter's unit ladder (the wire shape `parseEveryFromSchedule` reads back),
 * the shared table-header renderer, and the Save/Create button label.
 */
function job(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'zz-1',
    name: 'zzz-job',
    message: 'zzz',
    enabled: true,
    schedule: '',
    last_status: 'ok',
    ...overrides,
  }
}

describe('fmtSchedule', () => {
  it('prefers a raw cron expression verbatim', () => {
    expect(fmtSchedule(job({ cron_expr: '0 9 * * 1-5', every: 60 }))).toBe('0 9 * * 1-5')
  })

  it('renders sub-minute intervals in seconds', () => {
    expect(fmtSchedule(job({ every: 45 }))).toBe('45s')
    expect(fmtSchedule(job({ every: 59 }))).toBe('59s')
  })

  it('floors to whole minutes below an hour', () => {
    expect(fmtSchedule(job({ every: 60 }))).toBe('1m')
    expect(fmtSchedule(job({ every: 3599 }))).toBe('59m')
  })

  it('floors to whole hours below a day', () => {
    expect(fmtSchedule(job({ every: 3600 }))).toBe('1h')
    expect(fmtSchedule(job({ every: 7200 }))).toBe('2h')
  })

  it('floors to whole days at and beyond 86400s', () => {
    expect(fmtSchedule(job({ every: 86_400 }))).toBe('1d')
    expect(fmtSchedule(job({ every: 200_000 }))).toBe('2d')
  })

  it('formats a one-shot `at` timestamp as a date-time', () => {
    // 2026-08-14T00:00:00Z — TZ is pinned to UTC by the vitest config.
    const out = fmtSchedule(job({ at: 1_786_060_800 }))
    expect(out).not.toBe('—')
    expect(out).toMatch(/2026/)
  })

  it('falls back to an em dash when no schedule field is set', () => {
    expect(fmtSchedule(job())).toBe('—')
    // A zero `every` is not a schedule either (falsy) — it must not read as "0s".
    expect(fmtSchedule(job({ every: 0 }))).toBe('—')
  })
})

describe('renderThCells', () => {
  it('renders one <th> per column, keyed by header, with the shared + per-column class', () => {
    render(
      <table>
        <thead><tr>{renderThCells([{ h: 'zzz-name', w: 'w-40' }, { h: 'zzz-when', w: 'w-20' }])}</tr></thead>
      </table>,
    )
    const cells = screen.getAllByRole('columnheader')
    expect(cells).toHaveLength(2)
    expect(cells[0]).toHaveTextContent('zzz-name')
    expect(cells[0].className).toContain(TH_CLS)
    expect(cells[0].className).toContain('w-40')
    expect(cells[1].className).toContain('w-20')
  })
})

describe('SaveCreateLabel', () => {
  it('shows the create affordance (plus icon) when not editing', () => {
    const { container } = render(<SaveCreateLabel isEdit={false} saving={false} />)
    expect(screen.getByText('Create')).toBeInTheDocument()
    expect(container.querySelector('svg.lucide-plus')).toBeInTheDocument()
    expect(container.querySelector('svg.lucide-save')).not.toBeInTheDocument()
  })

  it('shows the save affordance (save icon) when editing', () => {
    const { container } = render(<SaveCreateLabel isEdit saving={false} />)
    expect(screen.getByText('Save')).toBeInTheDocument()
    expect(container.querySelector('svg.lucide-save')).toBeInTheDocument()
  })

  it('replaces the label with the in-flight text while saving, keeping the mode icon', () => {
    const { container } = render(<SaveCreateLabel isEdit saving />)
    expect(screen.getByText(/^Saving/)).toBeInTheDocument()
    expect(screen.queryByText('Save')).not.toBeInTheDocument()
    expect(container.querySelector('svg.lucide-save')).toBeInTheDocument()
  })
})

describe('cronUtils table classes', () => {
  it('exports distinct header and data cell classes', () => {
    expect(TH_CLS).not.toBe(TD_CLS)
    expect(TD_CLS).toContain('border-b')
  })
})

describe('cronUtils dow/cron formatting (smoke)', () => {
  it('expands a named range and formats a weekday expression', () => {
    expect(expandDow('MON-WED')).toEqual([1, 2, 3])
    expect(fmtCron('5 6 * * 1')).toBe('Mon 06:05')
    // Not five fields — returned untouched.
    expect(fmtCron('bogus')).toBe('bogus')
  })
})
