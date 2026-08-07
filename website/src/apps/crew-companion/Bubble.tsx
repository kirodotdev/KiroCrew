/**
 * The companion's speech bubble, with the timing and affordances each kind of
 * notification actually warrants.
 *
 * Ported from `BubbleOverlay` in the desktop app's PetWidget. What matters here is
 * that the bubble is NOT one uniform toast: `notificationPolicy` decides how long it
 * stays, whether a depleting bar shows that time passing, and whether there is a way
 * to act on it. A reminder the user set waits indefinitely; a break nudge clears
 * itself; blocked work offers a route in and never expires.
 *
 * Hovering pauses both the dismissal and the bar — reading a bubble must not be the
 * thing that loses it.
 */
import { X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import './bubble.css'
import { i18nT } from '../../i18n/t'
import { policyFor, isSticky, type NotifKind } from './notificationPolicy'

export interface BubbleProps {
  text: string
  kind: NotifKind
  /**
   * Where the downward arrow sits, in pixels from the bubble's left edge — the
   * `targetX - rect.left` the placement algorithm computes so the arrow points at
   * the companion's top edge. Omitted while the placement is still being measured,
   * in which case no arrow is drawn.
   */
  arrowLeft?: number
  /** Dismiss it. Called by the ✕, by a click on the body, and by the timer. */
  onDismiss: () => void
  /** Run the bubble's call to action, when its policy offers one. */
  onAction?: (action: 'breathe' | 'open-session') => void
}

/** Exit animation duration; the unmount waits exactly this long. */
const EXIT_MS = 300

export function Bubble({ text, kind, arrowLeft, onDismiss, onAction }: BubbleProps) {
  const policy = policyFor(kind)
  const [leaving, setLeaving] = useState(false)
  const [paused, setPaused] = useState(false)
  const timer = useRef<number | null>(null)

  /**
   * The dismissal timer, held while the pointer is over the bubble.
   *
   * Restarted rather than resumed on leave: the remaining time is not worth tracking
   * precisely, and a full window after the user looks away is the friendlier error.
   */
  useEffect(() => {
    if (policy.dismissMs === null || paused) return
    timer.current = window.setTimeout(() => setLeaving(true), policy.dismissMs)
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
  }, [policy.dismissMs, paused])

  // Let the exit animation finish before the bubble is actually removed.
  useEffect(() => {
    if (!leaving) return
    // 300ms, matching the 0.3s exit animation exactly. At 220 the element was
    // removed ~80ms before the slide-and-fade finished, so the bubble popped out
    // mid-animation instead of leaving.
    const t = window.setTimeout(onDismiss, EXIT_MS)
    return () => window.clearTimeout(t)
  }, [leaving, onDismiss])

  /*
   * A short first line is a KICKER, not part of the sentence.
   *
   * Carried over from the desktop app: when the text arrives as "context\nbody" and
   * that first line is short, it is set as a small upper-case label above the body
   * rather than run into it. This is what makes a completion read as
   * "PROJECT · BUCKET" over "Fix the parser" instead of one undifferentiated blob.
   * The 40-character bound is the source's, and it is what stops a genuine two-line
   * sentence being mangled into a heading.
   */
  const nl = text.indexOf('\n')
  const kicker = nl > 0 && nl <= 40 ? text.slice(0, nl).trim() : ''
  const body = kicker ? text.slice(nl + 1).trim() : text

  return (
    <div
      role="presentation"
      className={`cc-bubble-wrap${leaving ? ' cc-bubble-out' : ''}`}
      /*
       * The dismiss target is the WHOLE thing, including the tail and the empty
       * gutter beside a right-aligned CTA — as in the source, where the handler sits
       * on the outer wrapper. Scoped to the box alone, clicks just left of the CTA or
       * on the arrow did nothing, which reads as an unresponsive bubble.
       *
       * Gated on the SAME isSticky predicate as the ✕: a sticky bubble holds
       * UNRESOLVED work (approval / needs-input / error) and is cleared only
       * through its CTA. Ungated, a stray body click silently swallowed the
       * very notification promising "anything waiting on you always notifies".
       */
      onClick={isSticky(kind) ? undefined : () => setLeaving(true)}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      <div
        className="cc-bubble"
        role="status"
        data-kind={kind}
      >
        {/*
          Text and ✕ are FLEX SIBLINGS, so the ✕ takes its own space and the text
          shrinks to make room. Absolutely positioning it over the corner instead —
          which this port did — leaves it sitting on top of the words on a bubble as
          narrow as a two-character reminder.
        */}
        <div className="cc-bubble-text">
          {kicker ? <div className="cc-bubble-kicker">{kicker}</div> : null}
          <div className={kicker ? 'cc-bubble-body' : undefined}>{body}</div>
        </div>


        {/*
          No ✕ on a bubble that is holding UNRESOLVED work — blocked, needs input,
          needs approval. Those are cleared through their CTA, and a tidy-away button
          would invite dismissing the thing still waiting on you.

          Gated on `isSticky`, NOT on `dismissMs === null`. A reminder is also
          never-auto-dismissed, but it is not unresolved work: once it has spoken its
          piece you must be able to close it. Gating this on persistence is what left
          a fired reminder stuck on screen with no ✕ and no timeout.

          ALWAYS VISIBLE and keyboard-focusable — this button is the accessible
          dismiss control. It was hover-revealed once, which made a persistent
          reminder undismissable without a mouse; the wrapper's click-to-dismiss
          is a pointer-only convenience layered on top, never the only path.
        */}
        {!isSticky(kind) ? (
          <button
            type="button"
            className="cc-bubble-x"
            aria-label={i18nT('apps.crewCompanion.panel.close')}
            onClick={(e) => {
              e.stopPropagation()
              setLeaving(true)
            }}
          >
            <X size={12} aria-hidden="true" />
          </button>
        ) : null}

      </div>

      {/*
        The countdown sits BELOW the box, like the source's. Inside it, the bar was
        clipped by the 14px corner radius and read as a stray line drawn through the
        bubble. The animation is set on the track and inherited by its fill, so the
        `:hover` pause applies to the thing the user actually sees depleting.
      */}
      {policy.countdown && policy.dismissMs !== null ? (
        <span
          className="cc-bubble-countdown"
          style={{ animation: `ccBubbleCountdown ${policy.dismissMs}ms linear forwards` }}
          aria-hidden="true"
        />
      ) : null}

      {/*
        The CTA sits BELOW the bubble box, not inside it — a sibling, as in the source.
        Two reasons it cannot live in the box: the box is a flex ROW carrying the text
        and the ✕, so a block child would line up beside the words; and the box clips
        its overflow for the countdown bar. Stopping propagation still matters, since
        the whole bubble above is a dismiss target.
      */}
      {policy.ctaKey ? (
        <div className="cc-bubble-cta-row">
          <button
            type="button"
            className="cc-bubble-cta"
            onClick={(e) => {
              e.stopPropagation()
              if (policy.action) onAction?.(policy.action)
              setLeaving(true)
            }}
          >
            {i18nT(policy.ctaKey as Parameters<typeof i18nT>[0])}
          </button>
        </div>
      ) : null}

      {/*
        The arrow points down at the companion's top edge. Its horizontal offset is
        the placement algorithm's `targetX`, translated to the bubble's own frame; it
        lives outside the bubble box (which clips its own overflow for the countdown
        bar) so it is never cut off.
      */}
      {typeof arrowLeft === 'number' ? (
        <span className="cc-bubble-arrow" style={{ left: arrowLeft }} aria-hidden="true" />
      ) : null}
    </div>
  )
}
