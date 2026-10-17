import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { SettingsStepper } from '../components/settings'

/**
 * SettingsStepper centre readout.
 *
 * The bug this locks: with no `onReset`, the value cell rendered as
 * `<button disabled>`. That is a control that promises an action and then
 * refuses it — dimmed at 40% with a not-allowed cursor, announced to assistive
 * tech as an unavailable button — when nothing was ever on offer. It also
 * tripped any panel-wide "no disabled button" assertion the moment a caller
 * omitted the reset (PR #9041 hit exactly that in Settings → Chat).
 *
 * The contract: a reset-less stepper's readout is plain text; a stepper WITH
 * `onReset` keeps the clickable button. `disabled` still dims the whole
 * control either way.
 */
const disabledButtons = () => screen.queryAllByRole('button').filter(b => b.hasAttribute('disabled'))

describe('SettingsStepper readout', () => {
  it('renders the value as text, not a disabled button, when there is no onReset', () => {
    render(<SettingsStepper label="Retries" value={3} suffix="x" onIncrement={() => {}} onDecrement={() => {}} />)

    // MUTATION-VERIFIED: restoring `<button disabled={!onReset}>` for the
    // readout fails both assertions — the text lands in a BUTTON and that
    // button carries `disabled`.
    const readout = screen.getByText('3x')
    expect(readout.tagName).toBe('SPAN')
    expect(readout).not.toHaveAttribute('disabled')
    expect(readout.className).not.toMatch(/opacity-40/)
    expect(disabledButtons()).toHaveLength(0)
    // Only the two step actions are buttons.
    expect(screen.getAllByRole('button')).toHaveLength(2)
  })

  it('keeps the readout a clickable reset button when onReset is given', () => {
    const onReset = vi.fn()
    render(<SettingsStepper label="Zoom" value={110} suffix="%" onIncrement={() => {}} onDecrement={() => {}} onReset={onReset} />)

    const readout = screen.getByText('110%')
    expect(readout.tagName).toBe('BUTTON')
    expect(readout).not.toHaveAttribute('disabled')
    expect(readout).toHaveAttribute('title')
    fireEvent.click(readout)
    expect(onReset).toHaveBeenCalledTimes(1)
  })

  it('dims the whole control when disabled, in both shapes', () => {
    const { unmount } = render(
      <SettingsStepper label="Retries" value={3} onIncrement={() => {}} onDecrement={() => {}} disabled />
    )
    // Reset-less: the step buttons are disabled, the readout is dimmed text.
    expect(disabledButtons()).toHaveLength(2)
    const text = screen.getByText('3')
    expect(text.tagName).toBe('SPAN')
    expect(text.className).toMatch(/opacity-40/)
    unmount()

    const onReset = vi.fn()
    render(
      <SettingsStepper label="Zoom" value={110} onIncrement={() => {}} onDecrement={() => {}} onReset={onReset} disabled />
    )
    // With reset: all three are disabled buttons and the reset does not fire.
    expect(disabledButtons()).toHaveLength(3)
    fireEvent.click(screen.getByText('110'))
    expect(onReset).not.toHaveBeenCalled()
  })

  describe('with onSet (typed value)', () => {
    const renderTyped = (onSet = vi.fn(), value = 110) => {
      render(<SettingsStepper label="Zoom" value={value} suffix="%" min={50} max={300} onIncrement={() => {}} onDecrement={() => {}} onSet={onSet} />)
      return { onSet, input: screen.getByRole('spinbutton', { name: 'Zoom' }) as HTMLInputElement }
    }

    it('commits a typed integer on Enter', () => {
      const { onSet, input } = renderTyped()
      fireEvent.change(input, { target: { value: '115' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(onSet).toHaveBeenCalledWith(115)
      expect(onSet).toHaveBeenCalledTimes(1)
    })

    it('commits on blur, rounds, and shows the caller value afterwards', () => {
      const { onSet, input } = renderTyped()
      fireEvent.change(input, { target: { value: '500' } })
      fireEvent.blur(input)
      expect(onSet).toHaveBeenLastCalledWith(500)
      // The caller clamps; until it sends a new value the box shows the old one.
      expect(input.value).toBe('110')
      fireEvent.change(input, { target: { value: '112.6' } })
      fireEvent.blur(input)
      expect(onSet).toHaveBeenLastCalledWith(113)
    })

    it('reverts empty or non-numeric input to the value without calling onSet', () => {
      const { onSet, input } = renderTyped()
      fireEvent.change(input, { target: { value: '' } })
      fireEvent.keyDown(input, { key: 'Enter' })
      expect(input.value).toBe('110')
      fireEvent.change(input, { target: { value: 'abc' } })
      fireEvent.blur(input)
      expect(input.value).toBe('110')
      fireEvent.change(input, { target: { value: '125' } })
      fireEvent.keyDown(input, { key: 'Escape' })
      expect(input.value).toBe('110')
      expect(onSet).not.toHaveBeenCalled()
    })

    it('shows a new value from the caller', () => {
      const onSet = vi.fn()
      const props = { label: 'Zoom', suffix: '%', onIncrement: () => {}, onDecrement: () => {}, onSet }
      const { rerender } = render(<SettingsStepper {...props} value={110} />)
      rerender(<SettingsStepper {...props} value={125} />)
      expect((screen.getByRole('spinbutton') as HTMLInputElement).value).toBe('125')
    })

    it('keeps the static readout when onSet is absent', () => {
      render(<SettingsStepper label="Retries" value={3} onIncrement={() => {}} onDecrement={() => {}} />)
      expect(screen.queryByRole('spinbutton')).toBeNull()
    })
  })

  describe('reserveWidthFor (locale-proof readout width)', () => {
    // The bug this locks: a word readout ("Default") is wider than a short
    // number ("4"), so the readout box grew and shrank between them and the +
    // button shifted. A fixed `ch` floor fits one language's word and still
    // shifts for a longer translation ("Predeterminado"), so the readout
    // instead stacks an invisible, aria-hidden copy of each value it must fit
    // in the same grid cell as the shown value: the cell is as wide as the
    // widest copy in whatever locale and font the reader has.
    // MUTATION-VERIFIED: making StepperReadoutText return the bare text
    // whatever `reserve` holds (`if (true || !reserve?.length)`) fails the
    // first test: `getByText('4')` is then the readout box itself, with no
    // grid-cell class, and no hidden copy exists for `getByText('Default')`.
    it('stacks an invisible, aria-hidden copy of each reserved value in the cell of the shown value', () => {
      render(<SettingsStepper label="Cadence" value={4} reserveWidthFor={['Default', 1000]} onIncrement={() => {}} onDecrement={() => {}} />)
      const shown = screen.getByText('4')
      expect(shown.closest('[aria-hidden="true"]')).toBeNull()
      expect(shown.className).not.toMatch(/\binvisible\b/)
      expect(shown.className).toMatch(/\bcol-start-1 row-start-1\b/)
      expect(shown.parentElement?.className).toMatch(/\bgrid\b/)
      for (const reserved of ['Default', '1000']) {
        const copy = screen.getByText(reserved)
        expect(copy).toHaveAttribute('aria-hidden', 'true')
        expect(copy.className).toMatch(/\binvisible\b/)
        // The same grid cell as the shown value, so the cell is as wide as the widest.
        expect(copy.className).toMatch(/\bcol-start-1 row-start-1\b/)
        expect(copy.parentElement).toBe(shown.parentElement)
      }
    })

    it('renders the bare value, with no hidden copies, when the prop is absent', () => {
      render(<SettingsStepper label="Retries" value={3} suffix="x" onIncrement={() => {}} onDecrement={() => {}} />)
      const readout = screen.getByText('3x')
      // The text sits in the readout box itself: no wrapper, no copies.
      expect(readout.className).toMatch(/\bcursor-default\b/)
      expect(readout.children).toHaveLength(0)
      expect(document.querySelectorAll('[aria-hidden="true"]')).toHaveLength(0)
    })
  })
})
