// UnderlineTabs exists because a row of plain buttons is not a tablist: the
// three hand-rolled `border-b-2` rails in this repo have no keyboard model and no
// selected state for assistive tech. Those two properties are therefore what this
// file pins — the underline itself is decoration and is deliberately untested.
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import UnderlineTabs, {
  edgeEnabledIndex,
  nextEnabledIndex,
  type UnderlineTab,
} from '../components/UnderlineTabs'

type Key = 'a' | 'b' | 'c'

const TABS: Array<UnderlineTab<Key>> = [
  { key: 'a', label: 'Alpha' },
  { key: 'b', label: 'Beta', disabled: true, tooltip: 'Not yet' },
  { key: 'c', label: 'Gamma', count: 3 },
]

beforeAll(() => {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RO
})

const setup = (value: Key = 'a') => {
  const onChange = vi.fn()
  render(
    <UnderlineTabs<Key> tabs={TABS} value={value} onChange={onChange} ariaLabel="Planes" />,
  )
  return onChange
}

describe('nextEnabledIndex', () => {
  it('skips a disabled tab rather than landing on it', () => {
    // Focusing a tab that refuses selection strands the caret on a dead control.
    expect(nextEnabledIndex(TABS, 0, 1)).toBe(2)
  })

  it('wraps in both directions', () => {
    expect(nextEnabledIndex(TABS, 2, 1)).toBe(0)
    expect(nextEnabledIndex(TABS, 0, -1)).toBe(2)
  })

  it('stays put when every other tab is disabled', () => {
    const only: Array<UnderlineTab<Key>> = [
      { key: 'a', label: 'Alpha' },
      { key: 'b', label: 'Beta', disabled: true },
    ]
    expect(nextEnabledIndex(only, 0, 1)).toBe(0)
  })
})

describe('edgeEnabledIndex', () => {
  it('finds the first and last selectable tab', () => {
    expect(edgeEnabledIndex(TABS, 'first')).toBe(0)
    expect(edgeEnabledIndex(TABS, 'last')).toBe(2)
  })

  it('skips a disabled edge', () => {
    const tabs: Array<UnderlineTab<Key>> = [
      { key: 'a', label: 'Alpha', disabled: true },
      { key: 'b', label: 'Beta' },
    ]
    expect(edgeEnabledIndex(tabs, 'first')).toBe(1)
  })
})

describe('UnderlineTabs a11y contract', () => {
  it('is a labelled tablist of tabs', () => {
    setup()
    expect(screen.getByRole('tablist', { name: 'Planes' })).toBeInTheDocument()
    expect(screen.getAllByRole('tab')).toHaveLength(3)
  })

  it('marks exactly one tab selected', () => {
    setup('c')
    const selected = screen.getAllByRole('tab').filter(t => t.getAttribute('aria-selected') === 'true')
    expect(selected).toHaveLength(1)
    expect(selected[0]).toHaveTextContent('Gamma')
  })

  it('keeps the rail to ONE tab stop via a roving tabindex', () => {
    // A row of plain buttons costs the user one Tab press per screen name before
    // they can reach the content; the ARIA tabs pattern costs one for the rail.
    setup('a')
    const tabs = screen.getAllByRole('tab')
    expect(tabs.filter(t => t.getAttribute('tabindex') === '0')).toHaveLength(1)
    expect(tabs.filter(t => t.getAttribute('tabindex') === '-1')).toHaveLength(2)
  })

  it('marks a disabled tab aria-disabled but leaves it focusable with its reason', () => {
    setup()
    const beta = screen.getByText('Beta').closest('button')
    expect(beta).toHaveAttribute('aria-disabled', 'true')
    expect(beta).not.toBeDisabled()
    expect(beta).toHaveAttribute('title', 'Not yet')
  })
})

describe('UnderlineTabs selection', () => {
  it('selects on click', () => {
    const onChange = setup('a')
    fireEvent.click(screen.getByText('Gamma'))
    expect(onChange).toHaveBeenCalledWith('c')
  })

  it('refuses a click on a disabled tab', () => {
    const onChange = setup('a')
    fireEvent.click(screen.getByText('Beta'))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('moves with the arrow keys, skipping the disabled tab', () => {
    const onChange = setup('a')
    fireEvent.keyDown(screen.getAllByRole('tab')[0], { key: 'ArrowRight' })
    expect(onChange).toHaveBeenCalledWith('c')
  })

  it('jumps to the ends with Home and End', () => {
    const onChange = setup('c')
    fireEvent.keyDown(screen.getAllByRole('tab')[2], { key: 'Home' })
    expect(onChange).toHaveBeenCalledWith('a')
  })

  it('hides a zero count instead of rendering a 0 badge', () => {
    render(
      <UnderlineTabs<Key>
        tabs={[{ key: 'a', label: 'Alpha', count: 0 }]}
        value="a"
        onChange={vi.fn()}
        ariaLabel="Planes"
      />,
    )
    expect(screen.queryByText('0')).not.toBeInTheDocument()
  })
})
