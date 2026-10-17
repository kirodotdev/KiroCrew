/**
 * The crew window loads lazily, but its cover shows at once. While the chunk
 * loads, the guard must already isolate the covered local pane: an approval
 * behind the cover is neither reachable by the approval shortcuts nor
 * focusable.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import CrewWindowGuard from '../pages/chat/crew-window/CrewWindowGuard'
import { crewWindowShown } from '../pages/chat/crew-window/crewWindowStore'

describe('CrewWindowGuard', () => {
  it('marks the window shown and makes the covered pane inert until it unmounts', () => {
    const { unmount, getByTestId } = render(
      <div>
        <div data-testid="local-pane"><button type="button">Allow</button></div>
        <div data-crew-cover><CrewWindowGuard /></div>
      </div>,
    )
    expect(crewWindowShown()).toBe(true)
    expect(getByTestId('local-pane')).toHaveAttribute('inert')
    unmount()
    expect(crewWindowShown()).toBe(false)
  })

})
