/**
 * CrewmateLiveActivity — what a crewmate's chat shows while its turn runs: a
 * typing bubble (three bouncing dots in the crewmate's own gray bubble) with
 * ONE status line under it naming what the crewmate is doing right now
 * (#18238, #19997).
 *
 * It is the crewmate chat's whole working indicator; the pane does not mount
 * the chat page's `ChatFooter` (the ghost-pose carousel) beside it, or two
 * things would move at once. The bubble reads as a reply being typed, the way
 * iMessage draws it: same fill, corners and measure as the crewmate's message
 * bubbles (`.message-bubble.crewmate-bubble`, so a theme that restyles those
 * restyles this one), in the text column where the reply will land.
 *
 * It sits in the footer column, not in the transcript. That placement is the
 * point: a transcript row mounting and unmounting on every tool step hopped
 * the indicator up and down by a row each time. The caption is ONE fixed-height
 * row, mounted for the WHOLE live turn, whose text swaps in place — so nothing
 * under it moves while the crewmate works. The bubble folds away (height, not
 * unmount) while the reply's text is actively arriving — that streaming
 * bubble already carries the caret, and dots under it read as a second reply
 * coming — and while the caption says `stopping` or `compacting`, when no
 * reply is being typed at all; in those two phases the caption pulses (with
 * the footer's hourglass for compacting) so the chat still visibly works. The
 * same quiet rule `ChatFooter` uses (`useStreamIdle`) brings the dots back
 * when the model pauses mid-stream.
 *
 * The caption is the DM header pill's own reading (`useSlotActivity` →
 * `resolvePillActivity`, over the slot's live status record and the shared
 * `toolStatusLabel`), so the chat and the header name one moment the same way.
 * `stopping` and `compacting` are spelled here too — the footer that used to
 * say them is not mounted in a crewmate chat.
 */
import { useTranslation } from 'react-i18next'
import { motion, useReducedMotion } from 'framer-motion'
import { Hourglass } from 'lucide-react'
import { PILL_ACTIVITY_KEY, type PillActivity, type PillActivityKind } from '../../pages/members/pillActivity'
import { useStreamIdle } from '../../pages/chat/ChatFooter'
import { useHeldActivity } from './useHeldActivity'
import TypingDots from './TypingDots'
import { crewmateBubbleClass } from './crewmateBubbles'

export default function CrewmateLiveActivity({
  activity: live,
  streamingText = false,
  streamTick = 0,
}: {
  activity: PillActivity
  /** The newest row is the reply's text and the slot says it is streaming. */
  streamingText?: boolean
  /** Grows with each streamed chunk; stalling for `STREAM_IDLE_MS` means quiet. */
  streamTick?: number
}) {
  // Each step holds a moment so fast tool calls stay readable (useHeldActivity).
  const activity = useHeldActivity(live)
  const { t } = useTranslation()
  const reduce = useReducedMotion()
  const streamQuiet = useStreamIdle(streamTick, streamingText)
  // No reply is being typed while the user's own Stop is landing or the
  // context is being compacted: bouncing dots under "Stopping…" would say the
  // opposite of the caption, so the bubble folds for those two phases too.
  const idlePhase = activity.kind === 'stopping' || activity.kind === 'compacting'
  const dotsShown = (!streamingText || streamQuiet) && !idlePhase
  const label = activity.fullText ?? activity.text ?? t(PILL_ACTIVITY_KEY[activity.kind as Exclude<PillActivityKind, 'tool' | 'thinking'>])
  return (
    <div
      data-testid="crewmate-live-activity"
      data-activity={activity.kind}
      className="px-4 mx-auto w-full py-1 mt-3"
      style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
    >
      {/* The bubble's PARENT carries the fill token (`:has(>.message-bubble.crewmate-bubble)`
          in index.css), the same way a message row does. */}
      <motion.div
        className="flex flex-col items-start gap-1"
        initial={reduce ? false : { opacity: 0, y: 4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
      >
        {/* Folds by height so the caption under it slides, never jumps; stays
            mounted so the dots' animation phase and the row's identity survive
            a pause in the stream. Decorative: the caption is the live region. */}
        <motion.div
          data-testid="crewmate-typing-bubble"
          data-shown={dotsShown ? 'true' : 'false'}
          className="overflow-hidden"
          aria-hidden="true"
          initial={false}
          animate={{ height: dotsShown ? 'auto' : 0, opacity: dotsShown ? 1 : 0 }}
          transition={{ duration: reduce ? 0 : 0.2 }}
        >
          {/* Exactly one one-line reply tall, by construction rather than by a
              number: the same bubble classes and font scope a crewmate message
              gets (`AssistantMessage`'s hook classes + `crewmateBubbleClass`,
              imported, not copied) around the same
              tree the markdown renders — `.group` > an unclassed wrapper > the
              paragraph (`my-1`, line-height from message-font-size.css). The
              wrapper is load-bearing: the bubble zeroes `.group`'s first/last
              child margins, which is the wrapper, so the paragraph keeps its
              `my-1`, as a real reply's does. One invisible zero-width character
              gives the paragraph its line; the dots sit on that line. */}
          <div
            className={`message-bubble mc-message-font-scope msg-content leading-relaxed ${crewmateBubbleClass('single')}`}
            style={{ fontSize: 'var(--mc-message-font-size, 14px)' }}
          >
            <div className="group">
              <div>
                <p className="my-1 leading-6 flex items-center text-muted">
                  <span className="invisible w-0">{'\u200B'}</span>
                  <TypingDots />
                </p>
              </div>
            </div>
          </div>
        </motion.div>
        {/* One line, pinned: `h-5` + `truncate` so a long label can never add a
            second line and move anything below. */}
        {/* The live region is this STABLE wrapper, never the keyed span: a
            `role="status"` element replaced on every step is one some screen
            readers never announce. */}
        <div className="flex items-center h-5 min-w-0 w-full px-1 text-[13px] text-muted" role="status" aria-live="polite" title={label || undefined}>
          {/* Keyed by the text so a new step fades in over the old text's place;
              the old span simply leaves — no exit animation, nothing to shift. */}
          {/* With the dots folded, the caption is the only sign of work, so in
              those two phases it carries the motion itself: the same pulse (and
              hourglass for compacting) the chat page's footer gives them. */}
          <motion.span
            key={label}
            className={`min-w-0 truncate leading-5${idlePhase ? ' animate-pulse' : ''}`}
            initial={reduce ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.25 }}
          >
            {activity.kind === 'compacting' && <Hourglass className="lucide-inline" aria-hidden="true" />}
            {activity.kind === 'compacting' ? ' ' : ''}{label}
          </motion.span>
        </div>
      </motion.div>
    </div>
  )
}
