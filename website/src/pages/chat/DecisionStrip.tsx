import { memo, useId, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Check, ChevronRight, Puzzle, ThumbsDown, ThumbsUp } from 'lucide-react'

import { api, type DecisionFeedbackSide, type DecisionVerdictValue } from '../../api/client'
import { queryClient } from '../../api/queryClient'
import ErrorNotice from '../../components/ErrorNotice'
import { IconButton } from '../../components/ui'
import { fmtCompact, fmtList, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { DECISIONS_LIVE_POINT } from '../settings/decisionsPreview'
import {
  nextVerdict,
  recordedVerdict,
  rememberVerdict,
  type DecisionStripRecord,
} from './decisionRecord'
import { useRowDisclosure } from './rowDisclosure'

/** Two decimals, so `0.81` reads as a score and not as a rounded `0.8`. */
const confidence = (p: number) => fmtNumber(p, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/**
 * A skill list, or the word for an empty one — never a bare empty span.
 *
 * `type: 'unit'` renders "a, b" rather than the conjunction default's "a and b":
 * these are skill keys standing in a measurement line, not a sentence.
 */
function names(list: string[]): string {
  return list.length > 0 ? fmtList(list, { type: 'unit' }) : i18nT('pages.chat.decisionStrip.no_skills')
}

/**
 * One reader's verdict on one side of the comparison.
 *
 * Two thumbs, and they are ONE control: the verdict is single-choice
 * (`right` | `wrong` | none), which is the case `max-two-buttons-per-row` names
 * as not counting toward its cap. Pressing the lit thumb takes the answer back,
 * so a misclick is undoable without a third control.
 *
 * `label` is VISIBLE and required, and it lives here rather than at the call
 * sites so neither pair can ship without one. A bare thumb beside a line of text
 * does not say what it rates, and an `aria-label` answers that for a screen
 * reader only — `IconButton` renders no text and no tooltip of its own, so each
 * button also carries its meaning as a `title`.
 *
 * The answer is kept only after the server took it. An optimistic flip would
 * have to be rolled back on a failure, and the strip has an honest alternative:
 * the pair is disabled while the request is in flight, and a failure renders
 * beside it rather than silently reverting.
 */
function VerdictThumbs({
  turnId,
  side,
  label,
  rightLabel,
  wrongLabel,
}: {
  turnId: string
  side: DecisionFeedbackSide
  label: string
  rightLabel: string
  wrongLabel: string
}) {
  const [verdict, setVerdict] = useState<DecisionVerdictValue>(() => recordedVerdict(turnId, side))
  const mut = useMutation({
    mutationFn: (next: DecisionVerdictValue) => api.sendDecisionsFeedback(turnId, next, side),
    onSuccess: (_data, next) => {
      rememberVerdict(turnId, side, next)
      setVerdict(next)
    },
  }, queryClient)
  const press = (pressed: 'right' | 'wrong') => mut.mutate(nextVerdict(verdict, pressed))

  return (
    <span className="inline-flex items-center gap-1 shrink-0">
      <span className="shrink-0 opacity-75" data-testid={`decision-strip-rate-label-${side}`}>{label}</span>
      <IconButton
        aria-label={rightLabel}
        title={rightLabel}
        aria-pressed={verdict === 'right'}
        variant={verdict === 'right' ? 'active' : 'default'}
        disabled={mut.isPending}
        onClick={() => press('right')}
        data-testid={`decision-strip-right-${side}`}
      >
        <ThumbsUp className="lucide-inline" aria-hidden="true" />
      </IconButton>
      <IconButton
        aria-label={wrongLabel}
        title={wrongLabel}
        aria-pressed={verdict === 'wrong'}
        variant={verdict === 'wrong' ? 'active' : 'default'}
        disabled={mut.isPending}
        onClick={() => press('wrong')}
        data-testid={`decision-strip-wrong-${side}`}
      >
        <ThumbsDown className="lucide-inline" aria-hidden="true" />
      </IconButton>
      {/* No `askAgent`: this row is drawn inside every transcript host,
          including the panes and Crew Members threads whose composers hold an
          unsent draft in local state, and the hand-off navigates away and
          unmounts that subtree. Same reasoning as CompactionCard's failure
          branch. */}
      <ErrorNotice
        message={mut.isError ? i18nT('pages.chat.decisionStrip.feedback_failed') : null}
        variant="inline"
        testId={`decision-strip-feedback-error-${side}`}
      />
    </span>
  )
}

/** One labelled measurement in the expanded body. */
function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-1.5 min-w-0">
      <span className="shrink-0 opacity-75">{label}</span>
      <span className="text-text tabular-nums truncate">{value}</span>
    </div>
  )
}

/**
 * The transcript's receipt for one skill-selection decision.
 *
 * The record on the row is the ONLY condition. The Decisions (Jev) switch is not
 * read here, deliberately: a stamped record is history that already happened and
 * already sits on this machine, so drawing it sends nothing. The switch governs
 * whether a FUTURE turn may ask Jev. Gating the receipt on it instead would mean
 * a reader who tried the preview and switched it off loses the record of which
 * past replies were Jev-picked — which is the invisibility this strip exists to
 * remove, reintroduced for history.
 *
 * Collapsed it is one line: the point, who picked what, the score and how long
 * Jev took, and the prompt tokens the narrower set saved. When the two sides
 * picked the same skills the line says so and prints the set once, naming both
 * sides; when they differ it names each with its own list, because that
 * difference is the only thing on the line a reader can act on. Expanding adds
 * the question's own shape — how many candidates, how many characters of message
 * and history left the machine, how long the answer took, what was dropped — and
 * a second thumbs pair for the word-matching rule, so a reader can say the old
 * rule was the right one.
 *
 * Every row here is a measurement a reader can act on. A number that is the
 * same on every turn is furniture, not a measurement, so the strip carries no
 * row for how the menu was batched (it is asked in one question) and none for
 * how many prior turns were clipped (a fact about the read, on the call row).
 *
 * Expansion survives the row being recycled out of the virtualised transcript
 * (`useRowDisclosure`); the thumbs survive it through their own store.
 *
 * The feedback mutation is handed the SHARED query client explicitly rather than
 * reading one out of context, for the reason `app-sdk/appQuery.ts` gives: this
 * row is drawn by `app-sdk/messageRenderers`, whose whole contract is that a host
 * may render a transcript outside the dashboard's React root.
 */
const DecisionStrip = memo(function DecisionStrip({
  record,
  disclosureKey,
}: {
  record: DecisionStripRecord
  disclosureKey?: string
}) {
  // memo() bails out of the provider-level repaint; subscribe so a language
  // switch repaints this row's strings.
  useLanguageGeneration()
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const panelId = useId()

  const pointLabel = record.point === DECISIONS_LIVE_POINT
    ? i18nT('pages.chat.decisionStrip.point_skills_select')
    : record.point
  const rightJev = i18nT('pages.chat.decisionStrip.rate_right_jev')
  const wrongJev = i18nT('pages.chat.decisionStrip.rate_wrong_jev')
  const latency = record.latencyMs > 0
    ? i18nT('pages.chat.decisionStrip.latency_value', { ms: fmtNumber(record.latencyMs) })
    : null
  // Both numbers describe the answer, so they share one parenthetical rather
  // than each taking a segment of a line that already truncates. Joined with
  // `fmtList`, as is the egress row below: the separator between two list items
  // is a locale's decision, not this file's, and the two joins are the same
  // shape -- a short list of measurements read as one value.
  const scores = [record.p !== null ? confidence(record.p) : null, latency].filter(
    (part): part is string => part !== null,
  )
  // The legend names what the group CAN hold, and the group holds a different
  // pair depending on what the record carried — so all three cases get their own
  // sentence instead of one that describes a number that is not there.
  const scoresTitle = record.p !== null && latency !== null
    ? i18nT('pages.chat.decisionStrip.confidence_latency_title')
    : record.p !== null
      ? i18nT('pages.chat.decisionStrip.confidence_title')
      : i18nT('pages.chat.decisionStrip.latency_title')

  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted mt-1"
      data-testid="decision-strip"
      data-agree={record.agree}
      data-expanded={expanded}
    >
      <div className="flex items-center gap-1.5 px-2 py-1 min-w-0 text-[12px] leading-5">
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          aria-controls={panelId}
          // No aria-label: the line's own text names the button, and
          // aria-expanded carries the state — same as CompactionCard's toggle.
          className="flex-1 flex items-center gap-1.5 min-w-0 text-left hover:text-text transition-colors"
          data-testid="decision-strip-toggle"
        >
          <ChevronRight
            className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
            aria-hidden="true"
          />
          <Puzzle className="lucide-inline shrink-0" aria-hidden="true" />
          <span className="shrink-0 font-medium text-text">{pointLabel}{' \u00B7'}</span>
          <span className="truncate min-w-0">
            {record.agree ? (
              <>
                {/* A glyph alone read as "brazil succeeded". The word beside it
                    is what carries the meaning now; the `title` and the sr-only
                    text keep the full sentence for a hover and a screen reader,
                    since neither is reliably reached on its own. */}
                <span title={i18nT('pages.chat.decisionStrip.agreed')}>
                  <span className="sr-only">{i18nT('pages.chat.decisionStrip.agreed')}</span>
                  <Check className="lucide-inline text-muted" aria-hidden="true" />
                </span>{' '}
                <span data-testid="decision-strip-agreed-word">
                  {i18nT('pages.chat.decisionStrip.agreed_short')}
                </span>
                {' \u00B7 '}
                {/* Names BOTH sides. With one list and no names, the reader had
                    no way to tell Jev was involved in this turn at all. */}
                {i18nT('pages.chat.decisionStrip.agreed_named', { names: names(record.jev) })}
              </>
            ) : (
              <>
                {i18nT('pages.chat.decisionStrip.baseline_named', { names: names(record.baseline) })}
                {' \u00B7 '}
                {i18nT('pages.chat.decisionStrip.jev_named', { names: names(record.jev) })}
              </>
            )}
          </span>
          {scores.length > 0 && (
            <span
              className="shrink-0 tabular-nums"
              title={scoresTitle}
              data-testid="decision-strip-scores"
            >
              ({fmtList(scores, { type: 'unit' })})
            </span>
          )}
          {record.tokensSaved > 0 && (
            <span className="shrink-0 tabular-nums" data-testid="decision-strip-saved">
              {'\u00B7 '}
              {i18nT('pages.chat.decisionStrip.saved_tokens', { tokens: fmtCompact(record.tokensSaved) })}
            </span>
          )}
        </button>
        <VerdictThumbs
          turnId={record.turnId}
          side="jev"
          label={i18nT('pages.chat.decisionStrip.rate_jev')}
          rightLabel={rightJev}
          wrongLabel={wrongJev}
        />
      </div>
      {expanded && (
        <div id={panelId} className="px-2 pb-2 pt-0 text-[12px] leading-5 flex flex-col gap-1 min-w-0">
          {/* Both sets, unconditionally. The collapsed line prints one when the
              sides agreed, so the expanded body is where "these two are the
              same list" is something the reader can check rather than take. */}
          <Detail label={i18nT('pages.chat.decisionStrip.baseline_label')} value={names(record.baseline)} />
          <Detail label={i18nT('pages.chat.decisionStrip.jev_label')} value={names(record.jev)} />
          <Detail
            label={i18nT('pages.chat.decisionStrip.candidates_label')}
            value={fmtNumber(record.candidates)}
          />
          {/* ONE row for the egress, because the two halves are one fact: the
              message excerpt and the prior turns are everything the question
              sends. Naming only the history would leave the number at 0 at the
              shipped budget while the message that did leave went unmentioned.

              Drawn only for a record that states the message length, the same
              way the latency row below is. A record without it is one whose
              producer did not measure the excerpt, and "message 0 chars" over a
              question that carried one is the false receipt this row replaced. */}
          {record.messageChars !== null && (
            <Detail
              label={i18nT('pages.chat.decisionStrip.sent_label')}
              value={fmtList(
                [
                  i18nT('pages.chat.decisionStrip.sent_message', { chars: fmtNumber(record.messageChars) }),
                  i18nT('pages.chat.decisionStrip.sent_history', { chars: fmtNumber(record.historyChars) }),
                ],
                { type: 'unit' },
              )}
            />
          )}
          {latency !== null && (
            <Detail label={i18nT('pages.chat.decisionStrip.latency_label')} value={latency} />
          )}
          {record.dropped.length > 0 && (
            <Detail
              label={i18nT('pages.chat.decisionStrip.dropped_label')}
              value={fmtList(record.dropped.map(d => `${d.key} (${confidence(d.p)})`), { type: 'unit' })}
            />
          )}
          {/* No `askAgent`, for the draft reason the thumbs' notice states. */}
          <ErrorNotice
            message={record.error}
            title={i18nT('pages.chat.decisionStrip.error_title')}
            variant="inline"
            testId="decision-strip-error"
          />
          <div className="flex items-center gap-1.5 min-w-0 pt-0.5">
            <VerdictThumbs
              turnId={record.turnId}
              side="baseline"
              label={i18nT('pages.chat.decisionStrip.rate_baseline')}
              rightLabel={i18nT('pages.chat.decisionStrip.rate_right_baseline')}
              wrongLabel={i18nT('pages.chat.decisionStrip.rate_wrong_baseline')}
            />
          </div>
        </div>
      )}
    </div>
  )
})

export default DecisionStrip
