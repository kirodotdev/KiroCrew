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
  it('collapses to a one-line system row by default: label + expand word, no payload', () => {
    render(<NudgeCard message={makeMsg()} />)
    expect(screen.getByText('Auto-nudge · cycle 76')).toBeTruthy()
    expect(screen.getByText('Show instruction')).toBeTruthy()
    // Body is not rendered until expanded — this is the whole point of the row.
    expect(screen.queryByTestId('nudge-card-body')).toBeNull()
    // The payload's first line is NOT previewed on the collapsed row either: the
    // instruction text is machine-facing, and quoting even one line of it made
    // the row read as a message the user had sent.
    expect(screen.queryByText(/Babysit the KiroCrew/)).toBeNull()
    expect(screen.getByTestId('nudge-card-toggle').getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByTestId('nudge-card').getAttribute('data-expanded')).toBe('false')
  })

  it('is drawn as a quiet system line, not a card or bubble', () => {
    render(<NudgeCard message={makeMsg()} />)
    const root = screen.getByTestId('nudge-card')
    // No card chrome on the collapsed row: ring / card background belong to the
    // expanded payload panel only.
    expect(root.className).not.toMatch(/ring-1|bg-card/)
    expect(root.className).toMatch(/text-muted/)
    // Centred like the other system dividers, not left-aligned like a bubble.
    expect(root.firstElementChild?.className).toMatch(/justify-center/)
  })

  it('reveals the full instruction text when expanded, and the toggle word flips', () => {
    render(<NudgeCard message={makeMsg()} />)
    fireEvent.click(screen.getByTestId('nudge-card-toggle'))
    const body = screen.getByTestId('nudge-card-body')
    expect(body.textContent).toContain('second line of instructions')
    expect(screen.getByText('Hide instruction')).toBeTruthy()
    expect(screen.queryByText('Show instruction')).toBeNull()
    expect(screen.getByTestId('nudge-card-toggle').getAttribute('aria-expanded')).toBe('true')
    // The payload panel carries the card chrome the row itself does not.
    expect(body.className).toMatch(/ring-1/)
  })

  it('omits the loop button when no loop handler is supplied', () => {
    render(<NudgeCard message={makeMsg()} />)
    expect(screen.queryByTestId('nudge-card-open-loop')).toBeNull()
  })

  it('opens the loop popover via the loop button, which names its destination', () => {
    const onOpenLoop = vi.fn()
    render(<NudgeCard message={makeMsg()} onOpenLoop={onOpenLoop} />)
    const btn = screen.getByTestId('nudge-card-open-loop')
    expect(btn.textContent).toContain('View loop')
    // Tooltip distinguishes it from the Expand toggle beside it (UX review).
    expect(btn.getAttribute('title')).toBe("Open this auto-nudge loop's status and controls")
    expect(screen.getByTestId('nudge-card-toggle').getAttribute('title')).toBe('Show nudge instructions')
    fireEvent.click(btn)
    expect(onOpenLoop).toHaveBeenCalledTimes(1)
  })

  it('still renders a row when the cycle number is unknown', () => {
    render(<NudgeCard message={makeMsg({ content: 'bare text' })} />)
    expect(screen.getByText('Auto-nudge')).toBeTruthy()
  })
})
