import { useState } from 'react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SegmentedControl, { type Segment } from '../components/SegmentedControl'

type Key = 'alpha' | 'beta' | 'gamma'

const SEGMENTS: Array<Segment<Key>> = [
  { key: 'alpha', label: 'Alpha' },
  { key: 'beta', label: 'Beta', disabled: true, tooltip: 'Beta is unavailable' },
  { key: 'gamma', label: 'Gamma' },
]

function ControlledSegments({ initial = 'alpha', compact = false, collapse = false }: {
  initial?: Key
  compact?: boolean
  collapse?: boolean
}) {
  const [value, setValue] = useState<Key>(initial)
  return (
    <div style={{ width: collapse ? 0 : 480 }}>
      <SegmentedControl<Key>
        segments={SEGMENTS}
        value={value}
        onChange={setValue}
        ariaLabel="Result filter"
        compact={compact}
        collapse={collapse}
      />
    </div>
  )
}

beforeAll(() => {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub
})

afterEach(() => {
  vi.restoreAllMocks()
  cleanup()
})

describe('SegmentedControl keyboard interaction', () => {
  it('uses one tab stop and arrow keys skip disabled radios and wrap in full mode', async () => {
    const user = userEvent.setup()
    render(<ControlledSegments />)

    const alpha = screen.getByRole('radio', { name: 'Alpha' })
    const beta = screen.getByRole('radio', { name: 'Beta' })
    const gamma = screen.getByRole('radio', { name: 'Gamma' })
    expect(alpha).toHaveAttribute('tabindex', '0')
    expect(beta).toHaveAttribute('tabindex', '-1')
    expect(gamma).toHaveAttribute('tabindex', '-1')

    alpha.focus()
    await user.keyboard('{ArrowRight}')
    expect(gamma).toHaveFocus()
    // Moving focus does not select: the tab stop stays on the committed value.
    expect(gamma).toHaveAttribute('aria-checked', 'false')
    expect(alpha).toHaveAttribute('tabindex', '0')

    await user.keyboard('{Enter}')
    expect(gamma).toHaveAttribute('aria-checked', 'true')
    expect(gamma).toHaveAttribute('tabindex', '0')
    expect(alpha).toHaveAttribute('tabindex', '-1')

    await user.keyboard('{ArrowRight}')
    expect(alpha).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(gamma).toHaveFocus()
    expect(gamma).toHaveAttribute('aria-checked', 'true')
  })

  it('falls back to the first enabled tab stop and supports Home and End in compact mode', async () => {
    const user = userEvent.setup()
    render(<ControlledSegments initial="beta" compact />)

    const alpha = screen.getByRole('radio', { name: 'Alpha' })
    const beta = screen.getByRole('radio', { name: 'Beta' })
    const gamma = screen.getByRole('radio', { name: 'Gamma' })
    expect(alpha).toHaveAttribute('tabindex', '0')
    expect(beta).toHaveAttribute('tabindex', '-1')
    expect(gamma).toHaveAttribute('tabindex', '-1')

    alpha.focus()
    await user.keyboard('{End}')
    expect(gamma).toHaveFocus()
    await user.keyboard(' ')
    expect(gamma).toHaveAttribute('aria-checked', 'true')

    await user.keyboard('{Home}')
    expect(alpha).toHaveFocus()
    expect(alpha).toHaveAttribute('aria-checked', 'false')
  })

  it('keeps keyboard selection open in dropdown mode and returns focus after pointer selection', async () => {
    const user = userEvent.setup()
    vi.spyOn(window.HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(0)
    render(<ControlledSegments collapse />)

    await waitFor(() => expect(screen.queryAllByRole('radio')).toHaveLength(0))
    const toggle = screen.getByRole('button', { name: 'Alpha' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    await user.click(toggle)
    const group = screen.getByRole('radiogroup', { name: 'Result filter' })
    const alpha = within(group).getByRole('radio', { name: 'Alpha' })
    const beta = within(group).getByRole('radio', { name: 'Beta' })
    const gamma = within(group).getByRole('radio', { name: 'Gamma' })
    await waitFor(() => expect(alpha).toHaveFocus())
    expect(alpha).toHaveAttribute('tabindex', '0')
    expect(beta).toHaveAttribute('tabindex', '-1')
    expect(gamma).toHaveAttribute('tabindex', '-1')

    await user.keyboard('{ArrowDown}')
    expect(gamma).toHaveFocus()
    expect(gamma).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByRole('radiogroup', { name: 'Result filter' })).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    await user.keyboard('{Home}')
    expect(alpha).toHaveFocus()
    expect(alpha).toHaveAttribute('aria-checked', 'true')
    await user.keyboard('{End}')
    expect(gamma).toHaveFocus()

    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('radiogroup')).toBeNull())
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveFocus()

    await user.click(toggle)
    const reopenedGroup = screen.getByRole('radiogroup', { name: 'Result filter' })
    // Nothing was committed by the arrow keys, so reopening lands on Alpha.
    await waitFor(() => expect(within(reopenedGroup).getByRole('radio', { name: 'Alpha' })).toHaveFocus())
    await user.click(within(reopenedGroup).getByRole('radio', { name: 'Gamma' }))
    await waitFor(() => expect(screen.queryByRole('radiogroup')).toBeNull())
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveFocus()
    expect(toggle).toHaveAccessibleName('Gamma')
  })
})
