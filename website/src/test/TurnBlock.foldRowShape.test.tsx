/**
 * The restored fold row's shape (#14885, the revert of #14407).
 *
 * The pill this PR removes carried the only pin on the row's shape
 * (ToolGroupToggle.unified.test.tsx), so without these assertions the restored
 * row has none: nothing would catch a future edit that re-boxes it, and nothing
 * would catch the loss of `aria-expanded` — the one behaviour the revert keeps
 * from #14407, because a rotated chevron announces no state to a screen reader.
 */
import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

function makeTurn(items: TurnItem[], complete = true): Extract<DisplayItem, { kind: 'turn' }> {
  return { kind: 'turn', items, complete }
}

const renderItem = (it: TurnItem, i: number) => (
  <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
)

/** An interim fan-out region: folded in both modes, so one toggle is rendered. */
const foldedTurn = () => ({
  ...makeTurn([
    { kind: 'single', msg: { role: 'assistant', content: 'Two of three agents are in…', ts: '1' }, idx: 0 },
    { kind: 'single', msg: { role: 'assistant', content: 'and the third just landed', ts: '2' }, idx: 1 },
  ]),
  interim: true,
})

describe('TurnBlock — restored fold row shape', () => {
  it('announces its disclosure state with aria-expanded, both ways', () => {
    const { container } = render(<TurnBlock turn={foldedTurn()} renderItem={renderItem} />)
    const toggle = container.querySelector('button')!
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
  })

  it('is a bare row, not a pill: no card background and no ring', () => {
    const { container } = render(<TurnBlock turn={foldedTurn()} renderItem={renderItem} />)
    const cls = container.querySelector('button')!.className
    expect(cls).toContain('bg-transparent')
    expect(cls).not.toMatch(/\bbg-card\b/)
    expect(cls).not.toMatch(/\bring-1\b/)
  })

  it('leaves the visible label as the accessible name — no aria-label to drift from it', () => {
    // WCAG 2.5.3 label-in-name: a speech-input user says what they read, and the
    // label already states the action ("Worked through 2 steps" / "Hide reasoning").
    const { container } = render(<TurnBlock turn={foldedTurn()} renderItem={renderItem} />)
    const toggle = container.querySelector('button')!
    expect(toggle.hasAttribute('aria-label')).toBe(false)
    expect(toggle.textContent).toMatch(/step|reasoning|tool call/i)
  })

  it('hides the chevron from assistive tech — the label carries the meaning', () => {
    const { container } = render(<TurnBlock turn={foldedTurn()} renderItem={renderItem} />)
    expect(container.querySelector('button svg')!.getAttribute('aria-hidden')).toBe('true')
  })
})
