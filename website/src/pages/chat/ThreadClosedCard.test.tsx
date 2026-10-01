/**
 * The row a thread leaves in its parent when it ends.
 *
 * Two things are pinned here. The card states the close in its own voice rather
 * than as the crewmate saying "Thread ended.", and the slot the gateway
 * records as the back-link is pressable -- a recorded pointer nothing renders is
 * a pointer that does not exist for the reader.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ThreadClosedCard from './ThreadClosedCard'

describe('ThreadClosedCard', () => {
  it('names the thread and opens it', () => {
    const onOpen = vi.fn()
    render(<ThreadClosedCard title="The other eight" onOpen={onOpen} />)
    expect(screen.getByTestId('thread-closed-card').textContent).toContain('The other eight')
    fireEvent.click(screen.getByTestId('thread-closed-card-open'))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('offers no way in when the anchor that named the slot is gone', () => {
    // Nothing to open: the card still records that the thread ended, and does not
    // offer a control that would fail.
    render(<ThreadClosedCard title="The other eight" />)
    expect(screen.getByTestId('thread-closed-card')).toBeTruthy()
    expect(screen.queryByTestId('thread-closed-card-open')).toBeNull()
  })

})
