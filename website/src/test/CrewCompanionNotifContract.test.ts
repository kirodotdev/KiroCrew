/**
 * The per-kind notification contract, pinned kind by kind.
 *
 * Every row below was read back out of the desktop app this was ported from
 * (`src/shared/notificationPolicy.ts` and the bubble in `PetWidget.tsx`), not
 * inferred from the port. The timings and CTAs matched on the first pass; what did
 * NOT match was which bubbles offer a ✕, and that single predicate is why a fired
 * reminder sat on screen with no way to close it:
 *
 *   source:  {!sticky && hovered && <✕/>}      -- sticky = blocked / input / approval
 *   port:    {policy.dismissMs !== null && …}  -- i.e. "auto-dismisses at all"
 *
 * Those two read as synonyms and are not. A reminder never auto-dismisses (a time
 * you chose must not expire unseen) yet is not unresolved work, so it keeps its ✕.
 * `isPersistent` and `isSticky` now say which is which, and this file asserts both
 * so the next person cannot quietly swap one for the other.
 */
import { describe, it, expect } from 'vitest'

import {
  policyFor,
  isPersistent,
  isSticky,
  BREAK_MS,
  SESSION_DONE_MS,
  type NotifKind,
} from '../apps/crew-companion/notificationPolicy'

/** kind -> [dismissMs, countdown, hasCta, sticky] exactly as the source defines it. */
const CONTRACT: Array<[NotifKind, number | null, boolean, boolean, boolean]> = [
  // kind             dismissMs         countdown  cta    sticky
  ['break', BREAK_MS, true, false, false],
  ['break-breathe', BREAK_MS, true, true, false],
  ['reminder', null, false, false, false],
  ['session-input', null, false, true, true],
  ['session-error', null, false, true, false],
  ['approval', null, false, true, true],
  ['session-done', SESSION_DONE_MS, false, false, false],
  ['other', SESSION_DONE_MS, false, false, false],
]

describe('notification behaviour matches the app it was ported from', () => {
  for (const [kind, dismissMs, countdown, hasCta, sticky] of CONTRACT) {
    it(`${kind}: ${dismissMs === null ? 'never auto-dismisses' : `${dismissMs}ms`}`
      + `${countdown ? ' · countdown' : ''}${hasCta ? ' · CTA' : ''}`
      + `${sticky ? ' · sticky (no ✕)' : ' · closable'}`, () => {
      const p = policyFor(kind)
      expect(p.dismissMs).toBe(dismissMs)
      expect(p.countdown).toBe(countdown)
      expect(p.ctaKey !== null).toBe(hasCta)
      expect(isSticky(kind)).toBe(sticky)
      expect(isPersistent(kind)).toBe(dismissMs === null)
    })
  }

  it('a countdown bar is only ever claimed alongside a real timeout', () => {
    // A depleting bar with nothing to deplete towards would be a lie about waiting.
    for (const [kind] of CONTRACT) {
      const p = policyFor(kind)
      if (p.countdown) expect(p.dismissMs).not.toBeNull()
    }
  })

  it('a failure is persistent but NOT sticky', () => {
    /*
     * The distinction the port originally got wrong in the other direction.
     *
     * Sticky means "still waiting on YOU" — a question. `session-error` is not a
     * question: the work already stopped. Marked sticky it had no ✕ and held the
     * notification slot for the full bounded hold, so one failed turn muted every
     * notification behind it. The source ships it dismissible; so does this.
     */
    expect(isPersistent('session-error')).toBe(true)
    expect(isSticky('session-error')).toBe(false)
    // Its CTA still exists — a failure is worth opening even though it is closable.
    expect(policyFor('session-error').ctaKey).not.toBeNull()
  })

  it('separates "never auto-dismisses" from "cannot be dismissed by hand"', () => {
    // The regression in one line: reminder is persistent AND closable. If someone
    // re-derives one predicate from the other, this fails.
    expect(isPersistent('reminder')).toBe(true)
    expect(isSticky('reminder')).toBe(false)
    // Whereas unresolved work is both.
    expect(isPersistent('approval')).toBe(true)
    expect(isSticky('approval')).toBe(true)
  })

  it('every sticky kind offers a CTA, since that is its only way out', () => {
    // Note the implication runs one way only: session-error has a CTA and is not
    // sticky. A CTA is a route in; stickiness is about refusing to be tidied away.
    for (const [kind] of CONTRACT) {
      if (isSticky(kind)) expect(policyFor(kind).ctaKey).not.toBeNull()
    }
  })
})
