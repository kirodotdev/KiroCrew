/**
 * A sortable column header shows which way its column is sorted.
 *
 * The order used to be readable only through `aria-sort`: a sighted reader
 * could not tell which column the rows followed, or in which direction. Both
 * header components now draw one arrow, on the sorted column only: up for
 * ascending, down for descending. The arrow is decoration for assistive tech
 * (`aria-sort` already says it), so it must stay out of the header's name.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import SortableHeader, { SortableTableHead } from '../components/SortableHeader'
import type { SortState } from '../hooks/useSortableTable'
import { Table, TableHeader, TableRow } from '../components/ui/table'

const arrowIn = (el: HTMLElement) => el.querySelector('svg')

function renderPlain(sort: SortState, onToggle = vi.fn()) {
  render(
    <table><thead><tr>
      <SortableHeader label="Name" sortKey="name" sort={sort} onToggle={onToggle} />
      <SortableHeader label="Status" sortKey="status" sort={sort} onToggle={onToggle} />
    </tr></thead></table>,
  )
  return onToggle
}

function renderUi(sort: SortState) {
  render(
    <Table><TableHeader><TableRow>
      <SortableTableHead label="Name" sortKey="name" sort={sort} onToggle={() => {}} />
      <SortableTableHead label="Status" sortKey="status" sort={sort} onToggle={() => {}} />
    </TableRow></TableHeader></Table>,
  )
}

describe.each([
  ['SortableHeader', renderPlain],
  ['SortableTableHead', renderUi],
] as const)('%s sort arrow', (_name, renderHeaders) => {
  it('draws no arrow while nothing is sorted', () => {
    renderHeaders({ key: null, dir: null })
    for (const th of screen.getAllByRole('columnheader')) expect(arrowIn(th)).toBeNull()
  })

  it('draws an up arrow on the ascending column and none on the others', () => {
    renderHeaders({ key: 'name', dir: 'asc' })
    const sorted = screen.getByRole('columnheader', { name: 'Name' })
    expect(sorted).toHaveAttribute('aria-sort', 'ascending')
    expect(arrowIn(sorted)).toHaveClass('lucide-chevron-up')
    expect(arrowIn(screen.getByRole('columnheader', { name: 'Status' }))).toBeNull()
  })

  it('draws a down arrow on the descending column', () => {
    renderHeaders({ key: 'status', dir: 'desc' })
    const sorted = screen.getByRole('columnheader', { name: 'Status' })
    expect(sorted).toHaveAttribute('aria-sort', 'descending')
    expect(arrowIn(sorted)).toHaveClass('lucide-chevron-down')
    expect(arrowIn(screen.getByRole('columnheader', { name: 'Name' }))).toBeNull()
  })

  it('keeps the arrow out of the names a screen reader hears', () => {
    renderHeaders({ key: 'name', dir: 'asc' })
    // Exact names: the arrow adds nothing to the cell or to its button.
    const sorted = screen.getByRole('columnheader', { name: 'Name' })
    expect(arrowIn(sorted)).toHaveAttribute('aria-hidden', 'true')
    expect(screen.getByRole('button', { name: 'Name' })).toBeInTheDocument()
  })
})

describe('SortableHeader sort arrow follows a press', () => {
  it('sorts from the keyboard and still reports the column', async () => {
    const user = userEvent.setup()
    const onToggle = renderPlain({ key: 'name', dir: 'asc' })
    screen.getByRole('button', { name: 'Status' }).focus()
    await user.keyboard('{Enter}')
    expect(onToggle).toHaveBeenCalledWith('status')
  })
})
