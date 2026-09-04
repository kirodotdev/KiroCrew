import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import {
  TH_CLS, TD_CLS, renderThCells, SaveCreateLabel, expandDow, fmtCron,
} from '../utils/cronUtils'

/**
 * Coverage for the parts of cronUtils the existing suite skips: the shared
 * table-header renderer, and the Save/Create button label.
 */

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
    expect(fmtCron('5 6 * * 1')).toBe('6:05 AM · Mon')
    // Not five fields — returned untouched.
    expect(fmtCron('bogus')).toBe('bogus')
  })
})
