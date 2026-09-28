/**
 * #9699 — one affordance for "N tool calls are folded here" across both hosts.
 *
 * `TurnBlock`'s fold toggle and `CollapsibleToolGroup`'s header both render
 * through `ToolGroupToggle`, so a reader never meets a bare text row in one host
 * and a filled pill in the other for the same folded content. This file locks
 * the parts of that a reader can see or a screen reader can hear:
 * the same button classes, the same disclosure chevron, `aria-expanded` on
 * both, an accessible name a speech-input user can say, and — for TurnBlock —
 * the count and toggle behaviour it already had, unchanged.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

const TOGGLE = '[data-testid="tool-group-toggle"]'

const makeTurn = (items: TurnItem[], complete = true): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })
const renderItem = (it: TurnItem, i: number) => <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>

/** A settled turn with two distinct tool calls, one of them stopped (🔧 + 🚫 rows). */
const twoCalls = (): TurnItem[] => [
  { kind: 'single', msg: { role: 'tool', content: '🔧 Running: shell', ts: '1', meta: { tool_call_id: 'a' } }, idx: 0 },
  { kind: 'single', msg: { role: 'tool', content: '🚫 Stopped', ts: '2', meta: { tool_call_id: 'a' } }, idx: 1 },
  { kind: 'single', msg: { role: 'tool', content: '🔧 Running: fs_read', ts: '3', meta: { tool_call_id: 'b' } }, idx: 2 },
  { kind: 'single', msg: { role: 'assistant', content: 'Done.', ts: '4' }, idx: 3 },
]

const toggleOf = (c: HTMLElement) => c.querySelector<HTMLButtonElement>(TOGGLE)!

describe('ToolGroupToggle — both hosts render the same affordance', () => {
  it('TurnBlock and CollapsibleToolGroup emit a button with identical classes and chevron', () => {
    const turn = render(<TurnBlock turn={makeTurn(twoCalls())} renderItem={renderItem} />)
    const turnBtn = toggleOf(turn.container)
    turn.unmount()

    const group = render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    const groupBtn = toggleOf(group.container)

    expect(turnBtn.className).toBe(groupBtn.className)
    // The pill's look, not a bare text row: filled card, ring, mono type.
    expect(turnBtn.className).toMatch(/\bbg-card\b/)
    expect(turnBtn.className).toMatch(/\bring-1\b/)
    expect(turnBtn.className).toMatch(/\bfont-mono\b/)
    // One Lucide disclosure chevron in both (no text glyph), pointing right
    // while collapsed.
    expect(turnBtn.querySelector('svg.lucide-chevron-right')).not.toBeNull()
    expect(groupBtn.querySelector('svg.lucide-chevron-right')).not.toBeNull()
    expect(turnBtn.textContent).not.toContain('▶')
    expect(groupBtn.textContent).not.toContain('▶')
    expect(turnBtn.querySelector('.rotate-90')).toBeNull()
    expect(groupBtn.querySelector('.rotate-90')).toBeNull()
    // Both say "2 tool calls" with the wrench in front of it.
    expect(turnBtn.textContent).toContain('2 tool calls')
    expect(groupBtn.textContent).toContain('2 tool calls')
    expect(turnBtn.querySelector('svg.lucide-wrench')).not.toBeNull()
    expect(groupBtn.querySelector('svg.lucide-wrench')).not.toBeNull()
  })

  it('carries the a11y contract in both hosts: button role, aria-expanded, a sayable name', () => {
    // TurnBlock's visible label already states the action, so it IS the name
    // (WCAG 2.5.3 label-in-name): no aria-label, the state rides on aria-expanded.
    const turn = render(<TurnBlock turn={makeTurn(twoCalls())} renderItem={renderItem} />)
    const turnBtn = screen.getByRole('button', { name: '2 tool calls' })
    expect(turnBtn).toHaveAttribute('aria-expanded', 'false')
    expect(turnBtn.hasAttribute('aria-label')).toBe(false)
    fireEvent.click(turnBtn)
    expect(turnBtn).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toBe(turnBtn)
    turn.unmount()

    // The group's label is stateless ("2 tool calls" in both states), so its
    // name leads with the verb the label lacks.

    render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    const groupBtn = screen.getByRole('button', { name: /^Expand .*2 tool calls/ })
    expect(groupBtn).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(groupBtn)
    expect(groupBtn).toHaveAttribute('aria-expanded', 'true')
    expect(groupBtn.getAttribute('aria-label')).toMatch(/^Collapse /)
  })
})

describe('TurnBlock — behaviour behind the shared pill is unchanged', () => {
  it('counts distinct calls, not rows: a stopped call with its 🚫 sibling is one call', () => {
    const { container } = render(<TurnBlock turn={makeTurn(twoCalls())} renderItem={renderItem} />)
    expect(toggleOf(container).textContent).toContain('2 tool calls')
    expect(toggleOf(container).textContent).not.toContain('3 tool calls')
  })

  it('collapses the tool rows by default and expands them on click, with the glyph turning', () => {
    const { container } = render(<TurnBlock turn={makeTurn(twoCalls())} renderItem={renderItem} />)
    expect(screen.queryByTestId('item-0')).toBeNull()
    expect(screen.getByTestId('item-3')).toBeInTheDocument()
    fireEvent.click(toggleOf(container))
    expect(screen.getByTestId('item-0')).toBeInTheDocument()
    expect(toggleOf(container).textContent).toContain('Hide tool calls')
    expect(toggleOf(container).querySelector('.rotate-90')).not.toBeNull()
    // Re-collapse: the rows leave through AnimatePresence's exit animation, so
    // the state that flips synchronously is the toggle's own.
    fireEvent.click(toggleOf(container))
    expect(toggleOf(container)).toHaveAttribute('aria-expanded', 'false')
    expect(toggleOf(container).textContent).toContain('2 tool calls')
    expect(toggleOf(container).querySelector('.rotate-90')).toBeNull()
  })

  it('renders no toggle while the turn is still running', () => {
    const { container } = render(<TurnBlock turn={makeTurn(twoCalls(), false)} renderItem={renderItem} />)
    expect(container.querySelector(TOGGLE)).toBeNull()
  })

  it('the step folds use the same pill with the reasoning icon, not a wrench', () => {
    const { container } = render(<TurnBlock turn={makeTurn(twoCalls())} renderItem={renderItem} collapseAll />)
    const btn = toggleOf(container)
    expect(btn.className).toMatch(/\bbg-card\b/)
    expect(btn.textContent).toContain('Worked through 2 steps')
    expect(btn.querySelector('svg.lucide-sparkles')).not.toBeNull()
    expect(btn.querySelector('svg.lucide-wrench')).toBeNull()
  })

  it('keeps the tool_call label keys the two hosts already shared', () => {
    // Same i18n key on both sides, so a translation of "N tool calls" cannot
    // drift between hosts either.
    render(<TurnBlock turn={makeTurn(twoCalls())} renderItem={renderItem} />)
    const turnText = screen.getByTestId('tool-group-toggle').textContent
    render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    const texts = screen.getAllByTestId('tool-group-toggle').map(b => b.textContent)
    expect(texts[1]).toBe(turnText)
  })
})

describe('CollapsibleToolGroup — live states still own the indicator slot', () => {
  it('a pending approval shows the attention dot and ring instead of the glyph', () => {
    const { container } = render(
      <CollapsibleToolGroup count={1} hasPermission permissionMeta={{ tool_input: { command: 'rm -rf build' } }} onApprove={async () => undefined}>
        <div>row</div>
      </CollapsibleToolGroup>,
    )
    const btn = toggleOf(container)
    expect(btn.className).toMatch(/\bring-warn\b/)
    expect(btn.textContent).not.toContain('▶')
    expect(btn.querySelector('.animate-ping')).not.toBeNull()
  })

  it('running tools show the pulsing dot on the default ring', () => {
    const { container } = render(<CollapsibleToolGroup count={1} isRunning><div>row</div></CollapsibleToolGroup>)
    const btn = toggleOf(container)
    expect(btn.className).toMatch(/\bring-border\b/)
    expect(btn.textContent).not.toContain('▶')
    expect(btn.querySelector('.animate-pulse')).not.toBeNull()
  })
})
