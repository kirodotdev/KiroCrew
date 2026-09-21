/** The queue-edit error banner sits directly above the composer.
 *
 *  `errors-use-error-notice` is a blocking rule: an `ErrorNotice` must either offer the agent
 *  hand-off or record why it does not. Here it must not -- the hand-off navigates away and would
 *  destroy the unsaved draft the banner sits on top of -- so the decision is written down rather
 *  than left as an absent prop a later reader would take for an oversight.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const src = () => readFileSync(resolve(process.cwd(), 'src/components/QueueStack.tsx'), 'utf-8')

describe('the queue-edit error surface records its hand-off decision', () => {
  it('carries a No hand-off justification naming what it protects', () => {
    const s = src()
    const i = s.indexOf('<ErrorNotice')
    expect(i, 'premise: the surface exists').toBeGreaterThan(-1)
    const before = s.slice(Math.max(0, i - 220), i)
    expect(before, 'the decision must be stated above the notice').toMatch(/No hand-off/)
    expect(before, 'and must name the draft it protects').toMatch(/composer|draft/i)
  })

  it('does not silently enable the hand-off instead', () => {
    // Positive control: `askAgent` would ALSO satisfy the rule, but it is the wrong answer here --
    // it navigates away from an unsaved composer draft. Pin the choice, not merely the rule.
    const s = src()
    const start = s.indexOf('<ErrorNotice')
    expect(s.slice(start, s.indexOf('/>', start))).not.toMatch(/askAgent/)
  })
})

describe('the error outlives the cards it was raised on', () => {
  it('renders the stack when an edit error exists with no queued cards left', () => {
    // The last entry can start running DURING its own edit: the PATCH is rejected and the queue
    // empties in the same beat, so gating on card count alone unmounted the only report of it.
    const pane = readFileSync(resolve(process.cwd(), 'src/components/ChatPane.tsx'), 'utf-8')
    const i = pane.indexOf('<QueueStack')
    expect(i, 'premise: the pane renders the stack').toBeGreaterThan(-1)
    const gate = pane.slice(Math.max(0, i - 160), i)
    expect(gate, 'the gate must admit an error with an empty queue').toMatch(/queueEditError/)
  })
})
