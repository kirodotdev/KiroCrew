import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * The reply-thread footer is a sibling of the user-message root inside the
 * `items-end` row wrapper, and `ThreadFooter` sets its own `align-self:
 * flex-end` for a user row because the normal theme puts that bubble on the
 * right. `align-self` on the item beats `align-items` on the container, so CLI
 * mode's `align-items: flex-start` (the rule that moves the bubble left) left
 * the footer pinned to the far right of a full-width left-aligned bubble.
 *
 * jsdom cannot compute the real cascade for an attribute + `:has()` scope, so
 * both halves are guarded at the source level: the CSS rule that re-states the
 * side, and the component class it counteracts.
 */
const here = dirname(fileURLToPath(import.meta.url))

describe('cli-mode.css re-aligns the thread footer with its bubble', () => {
  const css = readFileSync(resolve(here, '../styles/cli-mode.css'), 'utf-8')

  it('has a CLI-scoped direct-child rule pulling the user-row footer to flex-start', () => {
    // One declaration block: the CLI scope, the `:has([data-role="user"])`
    // wrapper, a DIRECT `[data-testid="thread-footer"]` child, and a body that
    // sets align-self: flex-start. Whitespace-tolerant.
    const rule = /\[data-ui="cli"\][^{}]*:has\(\[data-role="user"\]\)[^{}]*>\s*\[data-testid="thread-footer"\]\s*\{[^}]*align-self:\s*flex-start\s*!important/
    expect(css).toMatch(rule)
  })

  it('neutralises the right-hand negative margin the normal theme applies', () => {
    // `-mr-1.5` nudges the button past the wrapper's right edge; left-aligned
    // it would instead push the footer 6px INTO the row, off the bubble's edge.
    const rule = /\[data-testid="thread-footer"\]\s*\{[^}]*margin-right:\s*0\s*!important/
    expect(css).toMatch(rule)
  })

  it('indents the assistant-row footer by the bar and padding its bubble carries', () => {
    // CLI mode indents each bubble by putting the bar and padding on the message
    // ROOT, and the footer is that root's SIBLING, so it misses the indent on the
    // assistant side too -- by 6px double bar + 10px padding. `margin-left: 10px`
    // plus the button's own `px-1.5` lands it on the bubble's edge; the browser
    // harness measures the result.
    const rule = /\[data-ui="cli"\][^{}]*:has\(>\s*\[data-role="assistant"\]\)[^{}]*>\s*\[data-testid="thread-footer"\]\s*\{[^}]*margin-left:\s*10px\s*!important/
    expect(css).toMatch(rule)
  })

  it('still targets the class ThreadFooter actually emits for a user row', () => {
    // The guard above is only load-bearing while the component keeps declaring
    // the side as `align-self`. If ThreadFooter stops emitting `self-end`, this
    // CSS rule is dead and must be removed with it.
    const tsx = readFileSync(resolve(here, '../pages/chat/ThreadFooter.tsx'), 'utf-8')
    expect(tsx).toContain('self-end')
    expect(tsx).toContain('self-start')
  })
})
