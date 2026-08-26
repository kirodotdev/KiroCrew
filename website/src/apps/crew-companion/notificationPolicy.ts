/**
 * How each kind of notification behaves — pure, so the rules are readable and
 * testable in one place instead of being spread across the bubble renderer and the
 * polling loop.
 *
 * The rules exist because these notifications are NOT equivalent, though an earlier
 * version of the companion treated them identically (one 6s bubble with a boolean
 * `sticky`):
 *
 *   - A break nudge is an invitation. It should wait long enough to be noticed while
 *     you finish a thought, then leave on its own without needing a click.
 *   - A reminder the user set themselves must not disappear on a timer. They asked
 *     to be told at that moment; silently expiring it defeats the point.
 *   - Something needing input or that errored is unresolved WORK. Auto-dismissing it
 *     would read as "handled", so it never times out and offers a way in.
 *   - "Session done" is pure FYI.
 *
 * Ported from the desktop app's `src/shared/notificationPolicy.ts`. The i18n keys
 * are rebased onto Kiro Crew's `apps.crewCompanion.*` namespace; the timings and the
 * persistent/transient split are unchanged, because they are the behaviour.
 */

export type NotifKind =
  | 'break' // water / stretch / look away
  | 'break-breathe' // the breathing suggestion, which carries a CTA
  | 'reminder' // user-set — never auto-dismisses
  | 'session-done'
  | 'session-input' // waiting on the user
  | 'session-error'
  | 'approval' // blocked on approval
  | 'other'

/** What a bubble's call-to-action does when clicked. */
export type BubbleAction = 'breathe' | 'open-session'

export interface BubblePolicy {
  /** Milliseconds until auto-dismiss, or null to stay until dealt with. */
  dismissMs: number | null
  /** Show a depleting bar. Only meaningful alongside a dismissMs. */
  countdown: boolean
  /** i18n key for the CTA label, or null for no button. */
  ctaKey: string | null
  action: BubbleAction | null
}

/**
 * "Session done" is a glance — 6s, unchanged from the desktop app because nothing
 * about it needed fixing.
 */
export const SESSION_DONE_MS = 6_000

/**
 * A break nudge lives far longer than a session notification: it is asking you to
 * change what you are doing, which takes longer to register than "your task
 * finished". Long enough to notice mid-sentence, short enough that an ignored nudge
 * clears itself rather than becoming another thing to dismiss.
 */
export const BREAK_MS = 45_000

export function policyFor(kind: NotifKind): BubblePolicy {
  switch (kind) {
    case 'break':
      return { dismissMs: BREAK_MS, countdown: true, ctaKey: null, action: null }

    case 'break-breathe':
      // The one break nudge with something to click: the exercise is right there.
      return {
        dismissMs: BREAK_MS,
        countdown: true,
        ctaKey: 'apps.crewCompanion.breathe.start',
        action: 'breathe',
      }

    case 'reminder':
      // The user asked to be told at this moment. It waits for them.
      return { dismissMs: null, countdown: false, ctaKey: null, action: null }

    case 'session-input':
    case 'session-error':
    case 'approval':
      // Unresolved work, and it says WHERE to resolve it rather than being a dead
      // end that announces a block and offers no way out.
      return {
        dismissMs: null,
        countdown: false,
        ctaKey: 'apps.crewCompanion.notif.open_session',
        action: 'open-session',
      }

    case 'session-done':
    case 'other':
    default:
      return { dismissMs: SESSION_DONE_MS, countdown: false, ctaKey: null, action: null }
  }
}

/** True when this kind must never disappear on its own. */
export function isPersistent(kind: NotifKind): boolean {
  return policyFor(kind).dismissMs === null
}

/**
 * True when this kind holds the slot and offers NO ✕ — it leaves by being resolved.
 *
 * Deliberately NOT the same predicate as `isPersistent`, though the two are easy to
 * confuse and conflating them is a real bug this file has already caused: a reminder
 * is persistent (it never auto-dismisses, because a time you chose must not expire
 * unseen) but it is NOT sticky, so it must still offer the hover ✕. Gating the ✕ on
 * persistence instead of stickiness left a fired reminder on screen with no way to
 * close it — the bubble simply could not be got rid of.
 *
 * Sticky is about UNRESOLVED WORK: blocked, needs input, needs approval. Offering a
 * tidy-away button there invites dismissing the thing you still have to do, so those
 * are cleared through their CTA. One definition, exported, because the slot logic and
 * the bubble UI must agree on it.
 */
export function isSticky(kind: NotifKind): boolean {
  /*
   * `session-error` is deliberately NOT here, matching the app this was ported from.
   *
   * Sticky means "still waiting on YOU": approval and needs-input are questions, and
   * a tidy-away button on a question invites dismissing the thing you still have to
   * answer. A failure is not a question — the work already stopped. Treating it as
   * sticky gave it no ✕ and let it hold the notification slot for the full bounded
   * hold, so a single failed turn muted everything behind it.
   */
  return kind === 'approval' || kind === 'session-input'
}
