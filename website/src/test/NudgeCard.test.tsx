import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import NudgeCard, { parseNudgeMessage, nudgeMatchesLoop } from '../pages/chat/NudgeCard'
import type { ChatMessage } from '../types'

const BODY = 'Babysit the KiroCrew bug-fix PRs to MERGE-READY\nsecond line of instructions'

function makeMsg(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'nudge',
    content: `[auto-nudge cycle 76]\n${BODY}`,
    cls: 'msg msg-nudge',
    ts: '2026-07-25T00:52:47.000Z',
    ...over,
  } as ChatMessage
}

describe('parseNudgeMessage', () => {
  it('parses cycle and body from the text tag', () => {
    expect(parseNudgeMessage(makeMsg())).toEqual({ cycle: 76, body: BODY })
  })

  it('prefers structured meta over the text tag', () => {
    const msg = makeMsg({ meta: { nudge: { cycle: 3, loop_id: 'l1', body: 'from meta' } } })
    expect(parseNudgeMessage(msg)).toEqual({ cycle: 3, body: 'from meta' })
  })

  it('derives the body from content when meta carries only cycle and loop_id', () => {
    // This is the shape the gateway actually writes — body is not duplicated.
    const msg = makeMsg({ meta: { nudge: { cycle: 76, loop_id: 'l1' } } })
    expect(parseNudgeMessage(msg)).toEqual({ cycle: 76, body: BODY })
  })

  it('degrades gracefully when there is no tag and no meta', () => {
    expect(parseNudgeMessage(makeMsg({ content: 'bare text' }))).toEqual({
      cycle: null,
      body: 'bare text',
    })
  })
})

describe('nudgeMatchesLoop', () => {
  const withLoop = (loop_id?: string) =>
    makeMsg({ meta: { nudge: { cycle: 76, ...(loop_id ? { loop_id } : {}) } } })

  it('matches when the row belongs to the active loop', () => {
    expect(nudgeMatchesLoop(withLoop('l1'), 'l1')).toBe(true)
  })

  it('does not match a successor loop bound to the same slot', () => {
    // A slot can outlive its loop: remove one, create another. An old card must
    // not open controls for the unrelated new loop.
    expect(nudgeMatchesLoop(withLoop('l1'), 'l2')).toBe(false)
  })

  it('does not match when no loop is active', () => {
    expect(nudgeMatchesLoop(withLoop('l1'), null)).toBe(false)
    expect(nudgeMatchesLoop(withLoop('l1'), undefined)).toBe(false)
  })

  it('does not match legacy rows with no loop_id', () => {
    expect(nudgeMatchesLoop(withLoop(), 'l1')).toBe(false)
    expect(nudgeMatchesLoop(makeMsg(), 'l1')).toBe(false)
  })
})

describe('NudgeCard', () => {
  it('collapses the payload to a one-line chip by default', () => {
    render(<NudgeCard message={makeMsg()} />)
    expect(screen.getByText('Auto-nudge · cycle 76')).toBeTruthy()
    // Body is not rendered until expanded — this is the whole point of the card.
    expect(screen.queryByTestId('nudge-card-body')).toBeNull()
    expect(screen.getByTestId('nudge-card-toggle').getAttribute('aria-expanded')).toBe('false')
  })

  it('reveals the full instruction text when expanded', () => {
    render(<NudgeCard message={makeMsg()} />)
    fireEvent.click(screen.getByTestId('nudge-card-toggle'))
    const body = screen.getByTestId('nudge-card-body')
    expect(body.textContent).toContain('second line of instructions')
    expect(screen.getByTestId('nudge-card-toggle').getAttribute('aria-expanded')).toBe('true')
  })

  it('omits the loop button when no loop handler is supplied', () => {
    render(<NudgeCard message={makeMsg()} />)
    expect(screen.queryByTestId('nudge-card-open-loop')).toBeNull()
  })

  it('opens the loop popover via the loop button', () => {
    const onOpenLoop = vi.fn()
    render(<NudgeCard message={makeMsg()} onOpenLoop={onOpenLoop} />)
    fireEvent.click(screen.getByTestId('nudge-card-open-loop'))
    expect(onOpenLoop).toHaveBeenCalledTimes(1)
  })

  it('still renders a chip when the cycle number is unknown', () => {
    render(<NudgeCard message={makeMsg({ content: 'bare text' })} />)
    expect(screen.getByText('Auto-nudge')).toBeTruthy()
  })
})
