import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import CrewmateMessage from '../pages/chat/CrewmateMessage'
import { crewmateRowClass, type CrewmateRunPosition } from '../components/chat/crewmateBubbles'

afterEach(() => cleanup())

const POSITIONS: readonly CrewmateRunPosition[] = ['single', 'start', 'cont', 'end']

describe('CrewmateMessage', () => {
  it.each(POSITIONS)('a %s message draws no author line: no avatar, no name, no time', (pos) => {
    render(
      <CrewmateMessage pos={pos}>
        <p>bubble</p>
      </CrewmateMessage>,
    )
    // The DM header names the speaker once; the message carries nothing but
    // the bubble (#16617).
    expect(screen.queryByTestId('crewmate-author')).toBeNull()
    expect(screen.queryByTestId('crew-avatar')).toBeNull()
    expect(document.querySelector('[title]')).toBeNull()
    expect(screen.getByText('bubble')).toBeInTheDocument()
  })

  it.each(POSITIONS)('a %s message has no avatar gutter: the bubble is the row\'s only child', (pos) => {
    render(
      <CrewmateMessage pos={pos}>
        <p>bubble</p>
      </CrewmateMessage>,
    )
    const row = screen.getByTestId('crewmate-message')
    expect(row.children).toHaveLength(1)
    expect(row.firstElementChild!.tagName).toBe('P')
    expect(row.className).not.toMatch(/pl-\[/)
  })

  it('places the bubble in the row its run position dictates', () => {
    render(
      <CrewmateMessage pos="cont">
        <p>bubble</p>
      </CrewmateMessage>,
    )
    const row = screen.getByTestId('crewmate-message')
    for (const cls of crewmateRowClass('cont').split(/\s+/).filter(Boolean)) {
      expect(row.classList.contains(cls)).toBe(true)
    }
  })
})
