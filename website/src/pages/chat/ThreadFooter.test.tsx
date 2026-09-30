import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import ThreadFooter from './ThreadFooter'
import type { LegacyThreadSummary, SessionThreadSummary } from '../../api/threads'

// The faces: the crewmate's is the real CrewAvatar (a canvas-free SVG seed);
// stubbed so the footer's contract -- who took part, in order -- reads as a
// list of labelled marks instead of pixels.
vi.mock('../../components/CrewAvatar', () => ({
  default: ({ seed }: { seed: string }) => <span data-testid="face-crewmate">{seed}</span>,
}))

const LEGACY: LegacyThreadSummary = {
  kind: 'legacy',
  count: 4,
  last_reply_ts: '2026-09-22T07:44:00Z',
  participants: ['user', 'assistant'],
}
const LIVE: SessionThreadSummary = {
  kind: 'session',
  thread_slot: 'chat-77-1758524400',
  title: 'The other eight',
  opened_by: 'user',
  opened_at: '2026-09-22T07:40:00Z',
  closed_at: null,
  summary_mid: null,
}

describe('ThreadFooter', () => {
  it('a live thread reads as a thread and its title, with no invented count', () => {
    render(<ThreadFooter summary={LIVE} crewmateName="Radar" onOpen={() => {}} />)
    const footer = screen.getByTestId('thread-footer')
    expect(footer).toHaveAttribute('data-thread-kind', 'session')
    expect(footer).toHaveTextContent('Thread')
    expect(footer).toHaveTextContent('The other eight')
    // A live thread is someone else's session. How many turns it holds is not
    // this row's business, and a number here would have to be made up.
    expect(footer).not.toHaveTextContent(/repl/i)
    expect(footer).not.toHaveTextContent('Ended')
  })

  it('says an ended thread has ended, in the word the End control uses', () => {
    render(<ThreadFooter summary={{ ...LIVE, closed_at: '2026-09-22T09:00:00Z', summary_mid: 'm-9' }} crewmateName="Radar" onOpen={() => {}} />)
    expect(screen.getByTestId('thread-footer')).toHaveTextContent('Ended')
  })

  it('draws a live thread even with no title', () => {
    // An untitled thread still needs its way back in, so the badge does not
    // depend on the title being there.
    render(<ThreadFooter summary={{ ...LIVE, title: '' }} crewmateName="Radar" onOpen={() => {}} />)
    expect(screen.getByTestId('thread-footer')).toHaveTextContent('Thread')
  })

  it('a version 1 thread keeps its count, its time and its faces in first-appearance order', () => {
    render(<ThreadFooter summary={LEGACY} crewmateName="Radar" onOpen={() => {}} />)
    const footer = screen.getByTestId('thread-footer')
    expect(footer).toHaveAttribute('data-thread-kind', 'legacy')
    expect(footer).toHaveTextContent('4 replies')
    expect(footer).toHaveTextContent(/Last reply/)
    // The user's mark comes first because the user replied first; the
    // crewmate's face is seeded with its name.
    const marks = Array.from(footer.querySelector('span')!.children)
    expect(marks).toHaveLength(2)
    expect(marks[0]).toHaveAttribute('aria-hidden', 'true')
    expect(marks[1]).toHaveTextContent('Radar')
  })

  it('reads "1 reply" for a single version 1 reply and omits the time when there is none', () => {
    render(
      <ThreadFooter summary={{ kind: 'legacy', count: 1, last_reply_ts: '', participants: ['assistant'] }} crewmateName="Radar" onOpen={() => {}} />,
    )
    const footer = screen.getByTestId('thread-footer')
    expect(footer).toHaveTextContent('1 reply')
    expect(footer).not.toHaveTextContent(/Last reply/)
  })

  it('is one button that opens the thread and sits on the side its bubble does', () => {
    const onOpen = vi.fn()
    const { rerender } = render(<ThreadFooter summary={LEGACY} crewmateName="Radar" onOpen={onOpen} />)
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
    rerender(<ThreadFooter summary={LEGACY} crewmateName="Radar" onOpen={onOpen} align="end" />)
    const flipped = screen.getByTestId('thread-footer')
    expect(flipped.className).toContain('self-end')
    expect(flipped.className).toContain('-mr-1.5')
    expect(flipped.className).not.toContain('-ml-1.5')
  })
})
