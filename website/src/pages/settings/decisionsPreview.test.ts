/**
 * `readDecisionsPreview` — the config shapes the card has to survive.
 *
 * The reader is a pure function precisely so these cases cost nothing to pin:
 * the interesting states are all "the config is not what this build expects",
 * and each one has a different correct answer.
 */
import { describe, it, expect } from 'vitest'

import { DECISION_POINTS, readDecisionsPreview } from './decisionsPreview'

describe('readDecisionsPreview', () => {
  it('reads a config with no decisions section as unsupported, never as off', () => {
    // The distinction is the whole point: "off" invites a click, "unsupported"
    // means the write would come back 400 from a gateway that has no such field.
    const view = readDecisionsPreview({ telemetry: { enabled: true } })
    expect(view.supported).toBe(false)
    expect(view.preview).toBe(false)
    expect(view.arms).toEqual([])
  })

  it('reads an unresolved or failed config query as unsupported', () => {
    for (const value of [undefined, null, 'nope', 42, []]) {
      expect(readDecisionsPreview(value).supported).toBe(false)
    }
  })

  it('reads a present section with the flag off as supported', () => {
    const view = readDecisionsPreview({ decisions: { preview: false } })
    expect(view.supported).toBe(true)
    expect(view.preview).toBe(false)
  })

  it('reads an absent flag inside a present section as off', () => {
    // The backend default is false, so a section that omits the key IS that
    // default — not an unsupported build.
    const view = readDecisionsPreview({ decisions: { points: {} } })
    expect(view.supported).toBe(true)
    expect(view.preview).toBe(false)
  })

  it('turns on for an exact true only', () => {
    expect(readDecisionsPreview({ decisions: { preview: true } }).preview).toBe(true)
    // A hand-edited truthy value is not consent to send message text off the
    // machine, so it reads as off rather than as "they probably meant yes".
    for (const sloppy of ['true', 1, 'yes', {}]) {
      expect(readDecisionsPreview({ decisions: { preview: sloppy } }).preview).toBe(false)
    }
  })

  it("lists the arms it finds in the card's order, and skips the ones it cannot print", () => {
    const view = readDecisionsPreview({
      decisions: {
        preview: true,
        points: {
          'cron.novelty': { arm: 'off' },
          'skills.select': { arm: 'shadow' },
          // No `arm` key at all: nothing to print as the arm this point is on,
          // so no row rather than a row reading "undefined".
          'skills.dedupe': { impl: 'llm' },
          // Not one of the three this release explains, so not listed.
          'skills.unknown': { arm: 'shadow' },
        },
      },
    })
    expect(view.arms).toEqual([
      { point: 'skills.select', arm: 'shadow' },
      { point: 'cron.novelty', arm: 'off' },
    ])
  })

  it('lists no arms when the section carries no points map', () => {
    expect(readDecisionsPreview({ decisions: { preview: true } }).arms).toEqual([])
  })

  it('names the three points this release reads at', () => {
    // Hard-coded on purpose — the shipped copy names exactly these three, so a
    // fourth arriving in config must not grow a row nothing explains.
    expect([...DECISION_POINTS]).toEqual(['skills.select', 'skills.dedupe', 'cron.novelty'])
  })
})
