/**
 * The steps fold must honour the preference in the FIRST render, not a frame
 * later.
 *
 * Switching sessions repainted every transcript with its steps unfolded and
 * folded them about two seconds afterwards — a full-height reflow on every
 * switch. The cause was upstream (`useLatchedRunning`: a stale running latch
 * stamped the incoming session's trailing turn incomplete), and these tests pin
 * the boundary it reached, so the fold's mount-time state is a checked contract
 * rather than something a future caller can quietly re-break:
 *
 *   - preference on  → the reasoning is inside a collapsed fold at mount
 *   - preference off → the reasoning renders inline at mount, tool rows fold
 *   - turn incomplete → no fold at all, which IS the frame that used to flash
 *
 * There is deliberately no `act()`, `waitFor` or timer advance anywhere below:
 * a state that only settles after one of those is the defect, so asserting on
 * the render `render()` returns is the whole point.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

const renderItem = (it: TurnItem, i: number) => (
  <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
)

/** Reasoning, then a tool call, then the answer — the shape every collapse rule
 *  in TurnBlock keys off. */
const items = (): TurnItem[] => [
  { kind: 'single', msg: { role: 'assistant', content: 'Checking the config first.', ts: '1' }, idx: 0 },
  { kind: 'single', msg: { role: 'tool', content: '🔧 read_file config.json', ts: '2' }, idx: 1 },
  { kind: 'single', msg: { role: 'assistant', content: 'The config is fine, nothing to change.', ts: '3' }, idx: 2 },
]

const turn = (complete: boolean): Extract<DisplayItem, { kind: 'turn' }> =>
  ({ kind: 'turn', items: items(), complete })

/** The element CollapsibleSection wraps its children in, and the attribute it
 *  carries exactly while it is hiding them. */
const fold = (c: HTMLElement) => c.querySelector('[style*="overflow: hidden"]')

describe('TurnBlock — the steps fold at mount', () => {
  it('mounts the reasoning collapsed when the preference is on', () => {
    const { container } = render(<TurnBlock turn={turn(true)} renderItem={renderItem} collapseAll={true} />)
    const section = fold(container) as HTMLElement
    expect(section).not.toBeNull()
    expect(section.getAttribute('data-collapsed')).toBe('true')
    expect(section).toContainElement(screen.getByTestId('item-0'))
    // The answer is never inside the fold, whatever the preference says.
    expect(section).not.toContainElement(screen.getByTestId('item-2'))
  })

  it('mounts the reasoning inline when the preference is off', () => {
    const { container } = render(<TurnBlock turn={turn(true)} renderItem={renderItem} collapseAll={false} />)
    // Preference off is "show intermediate reasoning between tool calls": the
    // prose renders in place and nothing is hidden behind a reasoning fold.
    // Only the tool row folds, and default mode folds by UNMOUNTING it.
    expect(screen.getByTestId('item-0')).toBeInTheDocument()
    expect(container.querySelector('[data-collapsed="true"]')).toBeNull()
    expect(screen.queryByTestId('item-1')).toBeNull()
  })

  it('renders no fold at all while the turn reads as incomplete', () => {
    // The flashed frame: a turn stamped incomplete has every step laid out raw
    // with no toggle, so believing a stale running flag for one session's
    // transcript is what produced the reflow.
    const { container } = render(<TurnBlock turn={turn(false)} renderItem={renderItem} collapseAll={true} />)
    expect(fold(container)).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(screen.getByTestId('item-0')).toBeInTheDocument()
    expect(screen.getByTestId('item-1')).toBeInTheDocument()
  })
})
