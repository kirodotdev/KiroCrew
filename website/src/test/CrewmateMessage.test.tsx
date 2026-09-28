import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import CrewmateMessage from '../pages/chat/CrewmateMessage'
import { CREWMATE_AVATAR_PX } from '../components/chat/crewmateBubbles'

// The avatar is a drawing; this test is about WHERE the author line and the
// bubble land, so it stands in for the picture and records its props.
vi.mock('../components/CrewAvatar', () => ({
  default: ({ seed, size }: { seed: string; size: number }) => (
    <span data-testid="crew-avatar" data-seed={seed} data-size={size} />
  ),
}))

afterEach(() => cleanup())

const radar = { name: 'Radar', avatar: { kind: 'seeded' } }
const TS = '2026-09-22T06:00:00Z'

describe('CrewmateMessage', () => {
  it('opens a run with the author line: avatar seeded by the name, the name, the time', () => {
    render(
      <CrewmateMessage crewmate={radar} pos="start" ts={TS}>
        <p>first bubble</p>
      </CrewmateMessage>,
    )
    const author = screen.getByTestId('crewmate-author')
    const avatar = screen.getByTestId('crew-avatar')
    expect(avatar.getAttribute('data-seed')).toBe('Radar')
    expect(avatar.getAttribute('data-size')).toBe(String(CREWMATE_AVATAR_PX))
    expect(author).toHaveTextContent('Radar')
    // Some rendering of the timestamp, with the full form on hover.
    const time = author.querySelector('[title]')
    expect(time).not.toBeNull()
    expect(time!.textContent).not.toBe('')
    expect(screen.getByText('first bubble')).toBeInTheDocument()
  })

  it('a single message is its own run and carries the author line too', () => {
    render(
      <CrewmateMessage crewmate={radar} pos="single" ts={TS}>
        <p>alone</p>
      </CrewmateMessage>,
    )
    expect(screen.getByTestId('crewmate-author')).toHaveTextContent('Radar')
  })

  it.each(['cont', 'end'] as const)('a %s message continues the run without an author line', (pos) => {
    render(
      <CrewmateMessage crewmate={radar} pos={pos} ts={TS}>
        <p>later bubble</p>
      </CrewmateMessage>,
    )
    expect(screen.queryByTestId('crewmate-author')).toBeNull()
    expect(screen.queryByTestId('crew-avatar')).toBeNull()
    // The bubble still sits in the text column right of the avatar gutter.
    const row = screen.getByTestId('crewmate-message')
    expect(row.lastElementChild!.className).toMatch(/pl-\[38px\]/)
    expect(screen.getByText('later bubble')).toBeInTheDocument()
  })

  it('an untimestamped opener shows the name and no time', () => {
    render(
      <CrewmateMessage crewmate={{ name: 'nova-sky' }} pos="start">
        <p>hi</p>
      </CrewmateMessage>,
    )
    const author = screen.getByTestId('crewmate-author')
    expect(author).toHaveTextContent('nova-sky')
    expect(author.querySelector('[title]')).toBeNull()
  })
})
