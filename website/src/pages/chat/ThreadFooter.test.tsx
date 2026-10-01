import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import ThreadFooter from './ThreadFooter'

// The faces: the crewmate's is the real CrewAvatar (a canvas-free SVG seed);
// stubbed so the footer's contract -- who took part, in order -- reads as a
// list of labelled marks instead of pixels.
vi.mock('../../components/CrewAvatar', () => ({
  default: ({ seed }: { seed: string }) => <span data-testid="face-crewmate">{seed}</span>,
}))

const SUMMARY = { count: 4, last_reply_ts: '2026-09-22T07:44:00Z', participants: ['user', 'assistant'] }

describe('ThreadFooter', () => {
  it('shows the count in words, the last-reply time and the faces in first-appearance order', () => {
    render(<ThreadFooter summary={SUMMARY} crewmateName="Radar" onOpen={() => {}} />)
    const footer = screen.getByTestId('thread-footer')
    expect(footer).toHaveTextContent('4 replies')
    expect(footer).toHaveTextContent(/Last reply/)
    // The user's mark comes first because the user replied first; the
    // crewmate's face is seeded with its name.
    const faces = footer.querySelector('span')!
    const marks = Array.from(faces.children)
    expect(marks).toHaveLength(2)
    expect(marks[0]).toHaveAttribute('aria-hidden', 'true')
    expect(marks[1]).toHaveTextContent('Radar')
  })

  it('reads "1 reply" for a single reply and omits the time when there is none yet', () => {
    render(
      <ThreadFooter summary={{ count: 1, last_reply_ts: '', participants: ['assistant'] }} crewmateName="Radar" onOpen={() => {}} />,
    )
    const footer = screen.getByTestId('thread-footer')
    expect(footer).toHaveTextContent('1 reply')
    expect(footer).not.toHaveTextContent(/Last reply/)
  })

  it('is one button that opens the thread and sits on the side its bubble does', () => {
    const onOpen = vi.fn()
    const { rerender } = render(<ThreadFooter summary={SUMMARY} crewmateName="Radar" onOpen={onOpen} />)
    const footer = screen.getByRole('button', { name: 'Open thread' })
    // The side is declared as `align-self` plus the matching negative margin,
    // which is what pulls the button's own `px-1.5` back off the bubble's text
    // edge. Both halves per side: a branch that kept one and lost the other
    // would line the footer up 6px inside the bubble it belongs to.
    expect(footer.className).toContain('self-start')
    expect(footer.className).toContain('-ml-1.5')
    expect(footer.className).not.toContain('-mr-1.5')
    fireEvent.click(footer)
    expect(onOpen).toHaveBeenCalledTimes(1)
    rerender(<ThreadFooter summary={SUMMARY} crewmateName="Radar" onOpen={onOpen} align="end" />)
    const flipped = screen.getByTestId('thread-footer')
    expect(flipped.className).toContain('self-end')
    expect(flipped.className).toContain('-mr-1.5')
    expect(flipped.className).not.toContain('-ml-1.5')
  })
})
