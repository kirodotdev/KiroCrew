import { fireEvent, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import FollowUpBar from '../components/FollowUpBar'

/**
 * A bare command-shaped chip used to send on one click and now fills the composer. The dashed
 * edge and the under-bar note are AT-REST signals, so a habituated quick-send user gets a
 * different outcome with nothing marking the moment the gesture differed. The chip acknowledges
 * the hold at the point of the click.
 */
describe('a refused click is acknowledged where it happens', () => {
  function clickFirstChip(option: string) {
    const { container } = render(
      <FollowUpBar
        options={[option, 'Summarise the thread instead']}
        recommended={null}
        picked={new Set<string>()}
        onSelect={vi.fn()}
        onSend={vi.fn()}
        quickSend
      />,
    )
    const chip = container.querySelector('button')!
    fireEvent.click(chip)
    return container
  }

  it('marks the chip when the click is refused a send', () => {
    const container = clickFirstChip('/clear')
    expect(container.querySelector('[data-held="true"]')).toBeTruthy()
  })

  it('marks a declined marker chip too, which is refused for the same reason', () => {
    const container = clickFirstChip('(recommended) /clear')
    expect(container.querySelector('[data-held="true"]')).toBeTruthy()
  })

  it('leaves an ordinary chip unmarked, so the mark means something', () => {
    // Positive control on the discriminator: an ordinary label is not refused, so a mark here
    // would make the signal meaningless rather than informative.
    const container = clickFirstChip('Run the tests again')
    expect(container.querySelector('[data-held="true"]')).toBeNull()
  })

  it('still calls onSelect, so the acknowledgement does not swallow the click', () => {
    const onSelect = vi.fn()
    const { container } = render(
      <FollowUpBar
        options={['/clear']}
        recommended={null}
        picked={new Set<string>()}
        onSelect={onSelect}
        onSend={vi.fn()}
        quickSend
      />,
    )
    fireEvent.click(container.querySelector('button')!)
    expect(onSelect).toHaveBeenCalledWith('/clear', expect.anything())
  })
})
