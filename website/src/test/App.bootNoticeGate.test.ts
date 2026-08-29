import { describe, it, expect } from 'vitest'

/** THE BLOCKING FINDING -- the notice stack must open for a boot-read
 *  failure on its own.
 *
 *  The boot-error notice and its retry control were nested inside a container gated on
 *  `agentSwitchNotice || sessionCloseFailure`. In the common ISOLATED case -- the slot list
 *  failed and nothing else is wrong -- both are false, so the container never rendered and
 *  the user got an empty sidebar with no error and no way to retry. */

const src = Object.values(
  import.meta.glob('../App.tsx', { query: '?raw', eager: true }) as Record<string, { default: string }>,
)[0].default

const stackGate = (): string => {
  const i = src.indexOf('data-testid="notice-stack"')
  expect(i).toBeGreaterThan(-1)
  // The gate is the conditional immediately above the container it guards.
  return src.slice(Math.max(0, i - 400), i)
}

describe('the notice stack opens for a boot-read failure alone', () => {
  /** The retry control is the notice's only affordance, so it carries the notice's normal text
   *  weight rather than the dimmest one on screen, and nothing dims it under the cursor: fading
   *  the one thing the user can act on reads as disabling it. */
  it('gives the boot retry control normal weight and never dims it on hover', () => {
    const i = src.indexOf('data-testid="boot-slots-retry"')
    expect(i).toBeGreaterThan(-1)
    const control = src.slice(Math.max(0, i - 400), i)
    expect(control).toMatch(/underline text-text/)
    expect(control, 'retry control dims on hover').not.toMatch(/hover:text-muted/)
    expect(control, 'retry control sits at the dimmest weight').not.toMatch(/"[^"]*\btext-muted\b/)
  })

  it('names the boot-error condition in the OUTER gate', () => {
    expect(stackGate()).toMatch(/showBootSlotsError/)
  })

  /** Control: the gate must still open for the two notices it already carried, so the
   *  fix is an addition rather than a replacement. */
  it('still names both pre-existing notices in the outer gate', () => {
    const gate = stackGate()
    expect(gate).toMatch(/agentSwitchNotice/)
    expect(gate).toMatch(/sessionCloseFailure/)
  })

  /** The condition is bound ONCE, so the outer gate and the inner render cannot drift
   *  apart into a container that opens with nothing inside it. */
  it('shares one binding between the outer gate and the notice', () => {
    expect(src).toMatch(
      /const showBootSlotsError = [\s\S]{0,160}?bootSlotsError[\s\S]{0,160}?bootSlotsErrorDismissed[\s\S]{0,160}?bootSlotsRecovered/,
    )
    const uses = src.match(/showBootSlotsError/g) ?? []
    expect(uses.length).toBeGreaterThanOrEqual(3)
  })
})
