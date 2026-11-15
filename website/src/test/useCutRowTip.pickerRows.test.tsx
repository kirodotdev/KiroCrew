// Two model ids that share a prefix differ only at their end. A picker row
// therefore cuts an id at its front, and a row with a cut line shows its texts in
// full in the shared bubble on hover and on keyboard focus.
vi.mock('@radix-ui/react-popover', async () => await import('./__mocks__/@radix-ui/react-popover'))

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { OPEN_DELAY_MS } from '../components/InstantTip'
import ModelDropdownList from '../components/ModelDropdownList'
import MultiSelect from '../components/MultiSelect'

const ID = 'us.anthropic.claude-fable-5-2[1m]'
const LABEL = 'claude-fable-5-2[1m]'

/** jsdom lays nothing out, so a cut line is one whose widths are stubbed. */
function cut(el: Element) {
  Object.defineProperty(el, 'scrollWidth', { configurable: true, value: 300 })
  Object.defineProperty(el, 'clientWidth', { configurable: true, value: 100 })
}

function expectFrontCut(span: Element, id: string) {
  expect(span).toHaveClass('truncate', '[direction:rtl]', 'text-left')
  const run = span.querySelector('bdi')
  expect(run).toHaveAttribute('dir', 'ltr')
  expect(run).toHaveTextContent(id)
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('ModelDropdownList rows', () => {
  it('cuts the id hint of a named row, and the id of an unnamed row, at the front', () => {
    render(<ModelDropdownList
      models={[{ name: ID, displayName: LABEL }, { name: 'us.anthropic.claude-fable-5-1[1m]' }]}
      activeModel="" onSelect={vi.fn()} />)
    expectFrontCut(document.querySelector('[data-model-id-hint]') as Element, ID)
    const unnamed = document.querySelector('[data-model-id="us.anthropic.claude-fable-5-1[1m]"]') as Element
    expectFrontCut(unnamed, 'us.anthropic.claude-fable-5-1[1m]')
    // A backend label is prose, cut at its end.
    expect(screen.getByText(LABEL)).not.toHaveClass('[direction:rtl]')
  })

  it('shows the label and the full id on hover of a row with a cut line', () => {
    render(<ModelDropdownList models={[{ name: ID, displayName: LABEL }]} activeModel="" onSelect={vi.fn()} />)
    const row = screen.getByRole('option')
    expect(row).not.toHaveAttribute('title')
    cut(document.querySelector('[data-model-id-hint]') as Element)
    fireEvent.mouseEnter(row)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent(`${LABEL}${ID}`)
    fireEvent.mouseLeave(row)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('shows the full id on keyboard focus of a row with a cut line', () => {
    render(<ModelDropdownList models={[{ name: ID }]} activeModel="" onSelect={vi.fn()} />)
    const row = screen.getByRole('option')
    cut(document.querySelector('[data-model-name]') as Element)
    act(() => { row.focus() })
    expect(screen.getByRole('tooltip')).toHaveTextContent(ID)
  })

  it('shows nothing on a row whose lines fit', () => {
    render(<ModelDropdownList models={[{ name: 'opus', displayName: 'Opus 5.5' }]} activeModel="" onSelect={vi.fn()} />)
    const row = screen.getByRole('option')
    fireEvent.mouseEnter(row)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    act(() => { row.focus() })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('MultiSelect rows', () => {
  function open() {
    render(
      <MultiSelect
        label="Selectable Models"
        options={[
          { value: ID, label: LABEL, hint: ID, description: 'Fable at 1M context' },
          { value: 'opus', label: 'Opus 5.5', hint: 'opus' },
        ]}
        selected={new Set()}
        onToggle={vi.fn()}
        summary="Selected 0 / 2"
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Selectable Models' }))
    return (name: string) => screen.getByRole('checkbox', { name }).closest('label') as HTMLElement
  }

  it('cuts the id hint at the front', () => {
    const rowOf = open()
    expectFrontCut(rowOf(LABEL).querySelector('[data-multi-select-hint]') as Element, ID)
  })

  it('shows the label, the full id and the description on hover of a row with a cut line', () => {
    const rowOf = open()
    const row = rowOf(LABEL)
    cut(row.querySelector('[data-multi-select-hint]') as Element)
    fireEvent.mouseEnter(row)
    act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS) })
    expect(screen.getByRole('tooltip')).toHaveTextContent(`${LABEL}${ID}Fable at 1M context`)
  })

  it('shows the full id on keyboard focus of a row with a cut line, and nothing on a row that fits', () => {
    const rowOf = open()
    act(() => { rowOf('Opus 5.5').focus() })
    expect(screen.queryByRole('tooltip')).toBeNull()
    const row = rowOf(LABEL)
    cut(row.querySelector('[data-multi-select-hint]') as Element)
    act(() => { row.focus() })
    expect(screen.getByRole('tooltip')).toHaveTextContent(ID)
  })
})
