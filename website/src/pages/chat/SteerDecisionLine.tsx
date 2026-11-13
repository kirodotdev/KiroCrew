import { memo } from 'react'
import { Target } from 'lucide-react'

import { fmtList, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import VerdictThumbs from './DecisionVerdictThumbs'
import type { SteerDecisionRecord } from './decisionRecord'

/** Two decimals, so `0.83` reads as a score and not as a rounded `0.8`. */
const confidence = (p: number) => fmtNumber(p, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/**
 * The transcript's receipt for one mid-turn handling decision.
 *
 * A sender who picked `Auto (Jev)` did not choose between steering the running
 * turn and queueing for the next one, so the row has to say which one happened and
 * who decided it -- otherwise the only visible difference between the two is a
 * badge the sender has no reason to read as a decision.
 *
 * ONE line, not a disclosure card like `DecisionStrip`. Everything this decision
 * produced fits on it: the path, the score, the latency. There is no second arm to
 * compare and no menu to expand, and a collapsed card that hides nothing is a
 * control that does nothing.
 *
 * The record on the row is the ONLY condition, the same rule the strip states: a
 * stamped record is history that already happened and already sits on this
 * machine, so drawing it sends nothing, and gating it on the current consent switch
 * would lose the receipt for every past send the moment the preview is turned off.
 *
 * The thumbs are `DecisionStrip`'s own pair, shared rather than copied. Side
 * `jev`: the record names one decision by one party, so there is no baseline arm to
 * rate -- what the product would have done without the seam is the `baseline`
 * field, and rating that would be rating a path this send did not take.
 */
const SteerDecisionLine = memo(function SteerDecisionLine({
  record,
  ownMessage,
}: {
  record: SteerDecisionRecord
  /** No running turn took this row as a steer: the text runs as its own message.
   *  `short` when the row already shows the requeued lifecycle line ("runs as its
   *  own message"), so the receipt only says who chose and the row does not say
   *  the outcome twice. `full` on a row with no such line: the row the drain
   *  writes for a requeued steer, or the queued row of a steer that was
   *  unavailable. Read only for a steer choice. */
  ownMessage?: 'short' | 'full'
}) {
  // memo() bails out of the provider-level repaint; subscribe so a language
  // switch repaints this row's strings.
  useLanguageGeneration()

  // The record is the DECISION, so the line says what was chosen rather than what
  // the delivery did. The two can differ: a chosen steer whose live client is gone
  // falls through to the queue, and a line reading "steered the running turn" over
  // a queued message would report the opposite of what happened. The row's own
  // steer badge and queue card are what describe the delivery.
  // One exception: on a row that runs as its own message, "interrupt this turn"
  // reads as a decision about the NEW turn. That row says Jev chose to
  // interrupt, as an intent, and leaves the outcome to the lifecycle line, or
  // states it when the row has none. It does not say "not applied": a steer
  // whose RPC died after the write is requeued too, and may have been read. A
  // queue choice already names what happened, so it keeps its sentence.
  const ownTurn = !!ownMessage && record.choice !== 'queue'
  const choice = record.choice === 'queue'
    ? i18nT('pages.chat.decisionStrip.steer_chose_queue')
    : ownTurn
      ? ownMessage === 'short'
        ? i18nT('pages.chat.decisionStrip.steer_chose_steer_requeued')
        : i18nT('pages.chat.decisionStrip.steer_chose_steer_own_message')
      : i18nT('pages.chat.decisionStrip.steer_chose_steer')
  const latency = record.latencyMs > 0
    ? i18nT('pages.chat.decisionStrip.latency_value', { ms: fmtNumber(record.latencyMs) })
    : null
  // Both numbers describe the answer, so they share one parenthetical, joined the
  // way the strip joins its own: the separator between two measurements read as one
  // value is a locale's decision, not this file's.
  // The score carries its own WORD. A bare "0.83" beside a sentence was read as
  // "no idea what this is" (UX review): the hover title is unreachable on touch and
  // for a screen reader it is one more unlabelled number on every decided send.
  const scores = [
    record.p !== null
      ? i18nT('pages.chat.decisionStrip.steer_confidence', { p: confidence(record.p) })
      : null,
    latency,
  ].filter((part): part is string => part !== null)
  // The legend names what the group actually holds, so all three cases get their
  // own sentence instead of one describing a number that is not there.
  const scoresTitle = record.p !== null && latency !== null
    ? i18nT('pages.chat.decisionStrip.confidence_latency_title')
    : record.p !== null
      ? i18nT('pages.chat.decisionStrip.confidence_title')
      : i18nT('pages.chat.decisionStrip.latency_title')

  return (
    <div
      // The own-message row wraps too, so on a very narrow pane the score and
      // thumbs move below the sentence instead of squeezing it to a word a line.
      className={`inline-flex items-center gap-1.5 text-[12px] leading-5 text-muted mb-1 pr-1 min-w-0${ownTurn ? ' flex-wrap' : ''}`}
      data-testid="steer-decision-line"
      data-choice={record.choice}
      data-ran-as-own-turn={ownTurn ? 'true' : undefined}
    >
      {/* The same icon the steer lifecycle indicators use, so the decision reads
          as part of that family rather than as an unrelated badge. */}
      <Target size={12} className="shrink-0" aria-hidden="true" />
      {/* The own-message sentence wraps instead of truncating: its last clause is
          the correction, and on a narrow pane the score and thumbs would cut it
          off and leave "interrupt the turn" as the visible claim. */}
      <span className={ownTurn ? 'min-w-0 break-words' : 'truncate min-w-0'} data-testid="steer-decision-choice">{choice}</span>
      {scores.length > 0 && (
        <span className="shrink-0 tabular-nums" title={scoresTitle} data-testid="steer-decision-scores">
          ({fmtList(scores, { type: 'unit' })})
        </span>
      )}
      <VerdictThumbs
        turnId={record.turnId}
        side="jev"
        // NOT the strip's "Jev" label: this line already opens with Jev's name, and
        // the pair beside it read as a second, unexplained mention of it (UX
        // review). The strip needs the name because two sides are rated there; here
        // there is one decision, so the label names the ACT instead.
        label={i18nT('pages.chat.decisionStrip.steer_rate_label')}
        rightLabel={i18nT('pages.chat.decisionStrip.steer_rate_right')}
        wrongLabel={i18nT('pages.chat.decisionStrip.steer_rate_wrong')}
      />
    </div>
  )
})

export default SteerDecisionLine
