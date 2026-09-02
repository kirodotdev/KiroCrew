import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { RepoLabel } from '../apps/issue-radar/api'
import LabelPicker from '../apps/issue-radar/components/LabelPicker'

const onToggle = vi.fn()
const onAddMany = vi.fn()

const LABELS: RepoLabel[] = [
  { name: 'zzq-needs-triage', color: 'd73a4a', description: 'awaiting a decision' },
  { name: 'zzq-good-first', color: '7057ff', description: '' },
  { name: 'zzq-chore', color: 'cfd3d7', description: '' },
]

beforeEach(() => {
  vi.clearAllMocks()
})

function renderPicker(over: Partial<React.ComponentProps<typeof LabelPicker>> = {}) {
  return render(
    <LabelPicker labels={LABELS} selected={[]} onToggle={onToggle} {...over} />,
  )
}

/** The chip buttons, in render order, ignoring the toolbar controls. */
function chipNames(): string[] {
  return Array.from(document.querySelectorAll('button'))
    .filter((b) => b.className.includes('rounded-full'))
    .map((b) => b.textContent?.replace(/\d+$/, '').trim() ?? '')
}

describe('LabelPicker — states before the grid', () => {
  it('reports loading rather than an empty grid', () => {
    renderPicker({ loading: true })
    expect(screen.getByText('Loading labels…')).toBeInTheDocument()
    expect(chipNames()).toEqual([])
  })

  it('shows the fetch error message', () => {
    renderPicker({ error: new Error('zzq-labels-unreachable') })
    expect(screen.getByText('zzq-labels-unreachable')).toBeInTheDocument()
  })

  it('prefers the caller-supplied empty text over the default', () => {
    renderPicker({ labels: [], emptyText: 'zzq-nothing-configured' })
    expect(screen.getByText('zzq-nothing-configured')).toBeInTheDocument()
  })

  it('falls back to its own empty text when the caller gives none', () => {
    renderPicker({ labels: [] })
    expect(screen.getByText(/no labels/i)).toBeInTheDocument()
  })
})

describe('LabelPicker — selection and filtering', () => {
  it('toggles the label a chip names', async () => {
    renderPicker()
    await userEvent.click(screen.getByText('zzq-chore'))
    expect(onToggle).toHaveBeenCalledWith('zzq-chore')
  })

  it('keeps the selected set together at the top, then sorts by name', () => {
    renderPicker({ selected: ['zzq-needs-triage'] })
    expect(chipNames()).toEqual(['zzq-needs-triage', 'zzq-chore', 'zzq-good-first'])
  })

  it('narrows the grid to substring matches on the name', async () => {
    renderPicker()
    await userEvent.type(screen.getByRole('textbox', { name: 'Filter labels' }), 'chore')
    expect(chipNames()).toEqual(['zzq-chore'])
  })

  it('renders a count only when the caller supplies a non-zero one', () => {
    renderPicker({ countByLabel: new Map([['zzq-chore', 4], ['zzq-good-first', 0]]) })
    const chore = screen.getByText('zzq-chore').closest('button') as HTMLButtonElement
    const first = screen.getByText('zzq-good-first').closest('button') as HTMLButtonElement
    expect(chore.textContent).toContain('4')
    // A zero count is noise on a role picker — the chip is not "used 0 times",
    // it is simply not applied anywhere.
    expect(first.textContent).toBe('zzq-good-first')
  })

  it('marks a selected chip and fills it with the label colour', () => {
    renderPicker({ selected: ['zzq-needs-triage'] })
    const chip = screen.getByText('zzq-needs-triage').closest('button') as HTMLButtonElement
    expect(chip.className).toContain('font-bold')
    expect(chip.style.backgroundColor).toBe('#d73a4a')
  })
})

describe('LabelPicker — suggestions', () => {
  const pattern = /triage/i

  it('offers a bulk add for the labels the pattern matches', async () => {
    renderPicker({ suggestPattern: pattern, onAddMany })
    await userEvent.click(screen.getByRole('button', { name: /Add 1 suggested/ }))
    expect(onAddMany).toHaveBeenCalledWith(['zzq-needs-triage'])
  })

  it('rings a suggested chip that is not already selected', () => {
    renderPicker({ suggestPattern: pattern })
    const chip = screen.getByText('zzq-needs-triage').closest('button') as HTMLButtonElement
    expect(chip.className).toContain('ring-1')
  })

  it('drops a suggestion once it is selected, so the button disappears', () => {
    renderPicker({ suggestPattern: pattern, onAddMany, selected: ['zzq-needs-triage'] })
    expect(screen.queryByRole('button', { name: /suggested/ })).toBeNull()
  })

  it('hides the bulk add when no handler was supplied', () => {
    // Without onAddMany there is nowhere for the click to go, so offering it
    // would be a dead control.
    renderPicker({ suggestPattern: pattern })
    expect(screen.queryByRole('button', { name: /suggested/ })).toBeNull()
  })
})

describe('LabelPicker — orphans', () => {
  it('surfaces a saved label the repo no longer has, so it can be cleared', async () => {
    renderPicker({ selected: ['zzq-renamed-away'] })
    expect(screen.getByText('Saved but no longer on this repo:')).toBeInTheDocument()
    await userEvent.click(screen.getByText('zzq-renamed-away'))
    expect(onToggle).toHaveBeenCalledWith('zzq-renamed-away')
  })

  it('says nothing about orphans when every saved label still exists', () => {
    renderPicker({ selected: ['zzq-chore'] })
    expect(screen.queryByText('Saved but no longer on this repo:')).toBeNull()
  })
})
