import { memo, useId } from 'react'
import { ArrowRight, Check, ChevronRight, Cpu, Puzzle } from 'lucide-react'

import ErrorNotice from '../../components/ErrorNotice'
import { fmtCompact, fmtList, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { DECISIONS_LIVE_POINT } from '../settings/decisionsPreview'
import {
  isModelRecord,
  type DecisionModelRecord,
  type DecisionRecord,
  type DecisionStripRecord,
} from './decisionRecord'
import VerdictThumbs from './DecisionVerdictThumbs'
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
 * The transcript's receipt for one model-routing decision.
 *
 * Collapsed it is one line: the point, the tier Jev answered and the model that
 * tier routed the turn to, the score, how long the decision took, and the model
 * the turn would otherwise have used. The last one is what makes the row
 * actionable -- "complex" alone says nothing a reader can disagree with, while
 * "complex, so opus-5 instead of opus-4.8" is a claim about this reply.
 *
 * ONE thumbs pair, side `jev`. There is no second answer to rate: the alternative
 * is not another judgement but the absence of one, so a `baseline` pair would ask
 * the reader to rate "whatever the session was on", which is not a decision
 * anybody made about this turn.
 *
 * Expanding adds the question's own shape -- how much conversation it carried and
 * how much was clipped -- and the failure category when there is one. Expansion
 * survives the row being recycled out of the virtualised transcript
 * (`useRowDisclosure`); the thumbs survive it through their own store.
 */
function ModelRouteStrip({
  record,
  disclosureKey,
}: {
  record: DecisionModelRecord
  disclosureKey?: string
}) {
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const panelId = useId()
  const rightJev = i18nT('pages.chat.decisionStrip.rate_right_model')
  const wrongJev = i18nT('pages.chat.decisionStrip.rate_wrong_model')
  // An unpinned tier applied no model, and the row says so rather than leaving a
  // gap where an id belongs: every tier ships unpinned, so this is the state most
  // readers see first, and it is what tells them there is something to pin.
  const chosen = record.modelChosen || i18nT('pages.chat.decisionStrip.model_unpinned')
  // A session on the backend's own default names no model, and `''` must not
  // render as an empty gap where a model id belongs.
  const baseline = record.baselineModel || i18nT('pages.chat.decisionStrip.model_default')
  // The switch was asked for and did not take: a backend that judges the model
  // value can exhaust its candidate ladder and stay put without failing. The row
  // names the model the turn actually ran on, because the durable record it is
  // drawn from is what an owner reads when deciding what to pin.
  const notApplied = record.applied
    ? null
    : i18nT('pages.chat.decisionStrip.model_not_applied', {
        model: record.modelUsed || baseline,
      })

  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted mt-1"
      data-testid="decision-strip-model"
      data-tier={record.tier}
      data-pinned={record.modelChosen ? 'true' : 'false'}
      data-applied={record.applied ? 'true' : 'false'}
      data-expanded={expanded}
    >
      <div className="flex items-center gap-1.5 px-2 py-1 min-w-0 text-[12px] leading-5">
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          aria-controls={panelId}
          // No aria-label: the line's own text names the button, and
          // aria-expanded carries the state — same as the skill strip's toggle.
          className="flex-1 flex items-center gap-1.5 min-w-0 text-left hover:text-text transition-colors"
          data-testid="decision-strip-model-toggle"
        >
          <ChevronRight
            className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
            aria-hidden="true"
          />
          <Cpu className="lucide-inline shrink-0" aria-hidden="true" />
          <span className="shrink-0 font-medium text-text">
            {i18nT('pages.chat.decisionStrip.point_model_route')}{' \u00B7'}
          </span>
          <span className="truncate min-w-0" data-testid="decision-strip-model-pick">
            {i18nT('pages.chat.decisionStrip.model_tier', { tier: record.tier })}
            {' '}
            {/* The arrow is decoration: the sentence either side already reads
                "tier, then model", so a screen reader gains nothing from it. */}
            <ArrowRight className="lucide-inline" aria-hidden="true" />
            {' '}
            <span className={record.modelChosen ? 'font-mono text-text' : 'text-muted'}>{chosen}</span>
            {notApplied && (
              <span className="text-muted" data-testid="decision-strip-model-not-applied">
                {' '}
                {notApplied}
              </span>
            )}
          </span>
          {record.p !== null && (
            <span
              className="shrink-0 tabular-nums"
              title={i18nT('pages.chat.decisionStrip.confidence_title')}
              data-testid="decision-strip-model-confidence"
            >
              ({confidence(record.p)})
            </span>
          )}
          <span className="shrink-0 tabular-nums" data-testid="decision-strip-model-latency">
            {i18nT('pages.chat.decisionStrip.model_latency', {
              ms: fmtNumber(record.latencyMs),
            })}
          </span>
          {/* No baseline caption. The line says which tier this turn was put in
              and which model that landed on, which is the whole decision. The
              model the turn would OTHERWISE have run on is not a second fact on a
              routed session: it is the previous turn's tier, so the caption read
              as a "default" that changes every turn. It stays on the record and in
              the expanded panel, where it is labelled for what it is. */}
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
          <Detail label={i18nT('pages.chat.decisionStrip.model_tier_label')} value={record.tier} />
          {/* Labelled "Model used", so on a switch that did not take it names what the
              turn RAN on, not what was asked for -- the collapsed line above still
              carries the ask, so nothing is hidden by preferring the truth here. */}
          <Detail
            label={i18nT('pages.chat.decisionStrip.model_chosen_label')}
            value={record.applied ? chosen : record.modelUsed || baseline}
          />
          <Detail label={i18nT('pages.chat.decisionStrip.model_baseline_label')} value={baseline} />
          <Detail
            label={i18nT('pages.chat.decisionStrip.history_chars_label')}
            value={fmtNumber(record.historyChars)}
          />
          <Detail
            label={i18nT('pages.chat.decisionStrip.truncated_turns_label')}
            value={fmtNumber(record.truncated)}
          />
          {/* No hand-off: the unsaved composer draft in the thread this strip is
              rendered inside. The hand-off navigates and unmounts that subtree, so
              offering it here would trade a routing note for text the user typed and
              has not sent. Nothing is lost by withholding it either: the turn already
              ran on the session's own model, and the one actionable case -- a pinned id
              this account cannot run -- is fixed in `decisions.model_route`. */}
          <ErrorNotice
            message={record.error}
            title={i18nT('pages.chat.decisionStrip.error_title')}
            variant="inline"
            testId="decision-strip-model-error"
          />
        </div>
      )}
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
 * Both thumbs pairs are `DecisionVerdictThumbs`, shared with the tool card's risk
 * badge, so what a press sends and what a failure looks like are one
 * implementation rather than two.
 */
const DecisionStrip = memo(function DecisionStrip({
  record,
  disclosureKey,
}: {
  record: DecisionRecord
  disclosureKey?: string
}) {
  // memo() bails out of the provider-level repaint; subscribe so a language
  // switch repaints this row's strings. Called before the point branch below so
  // BOTH rows subscribe — a hook inside one branch would be a conditional hook.
  useLanguageGeneration()
  if (isModelRecord(record)) {
    return <ModelRouteStrip record={record} disclosureKey={disclosureKey} />
  }
  return <SkillSelectStrip record={record} disclosureKey={disclosureKey} />
})

function SkillSelectStrip({
  record,
  disclosureKey,
}: {
  record: DecisionStripRecord
  disclosureKey?: string
}) {
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
}

export default DecisionStrip
