import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import ThreadFooter from './ThreadFooter'
import type { LegacyThreadSummary } from '../../api/threads'

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

describe('ThreadFooter', () => {
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
