import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * CLI mode flattens the UserMessage root to `display: block` and the bubble's
 * `.msg-content` to `inline-block`, so the steer badges ("Steering…",
 * "Steered into the running turn", requeued) — inline-flex boxes that stack
 * above the bubble in the normal theme's flex-col root — flowed onto the SAME
 * line as the message text. cli-mode.css must promote direct-child badge
 * boxes back to block level. jsdom cannot compute the real cascade for the
 * attribute + :has() scope, so this guards the rule at the source level.
 */
describe('cli-mode.css keeps steer badges on their own line', () => {
  const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../styles/cli-mode.css'),
    'utf-8',
  )

  it('has a CLI-scoped direct-child rule promoting .inline-flex badges to flex', () => {
    // One declaration block: the user-root scope selecting direct .inline-flex
    // children, whose body sets display:flex. Whitespace-tolerant.
    const rule = /\[data-ui="cli"\][^{}]*\[data-role="user"\][^{}]*>\s*\.inline-flex\s*\{[^}]*display:\s*flex\s*!important/
    expect(css).toMatch(rule)
  })
})
