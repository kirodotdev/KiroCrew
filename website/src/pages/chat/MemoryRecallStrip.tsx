import { memo, useId, useState } from 'react'
import { Brain, ChevronRight } from 'lucide-react'

import ErrorNotice from '../../components/ErrorNotice'
import { fmtCompact, fmtList, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { type MemoryRecallRecord } from './decisionRecord'
import VerdictThumbs from './DecisionVerdictThumbs'
import { useRowDisclosure } from './rowDisclosure'
import MemoryPopover from './MemoryPopover'

/** Two decimals, so `0.81` reads as a score and not as a rounded `0.8`. */
const confidence = (p: number) => fmtNumber(p, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** Clipped only when it actually overflows — a short id keeps its whole self rather
 *  than gaining a trailing ellipsis that says nothing was cut. */
const clipId = (id: string) => (id.length > 12 ? `${id.slice(0, 12)}…` : id)

const idChipClass =
  'inline-block bg-accent/10 hover:bg-accent/20 text-accent px-2 py-0.5 rounded text-[11px] font-mono cursor-pointer transition-colors'

/**
 * A memory-id list, or the word for an empty one — never a bare empty span.
 *
 * Each id is a chip a reader can click to open that memory's text in `MemoryPopover`.
 * The chips are identifiers in a list, not peer actions, so they wrap rather than fold.
 */
function IdList({ ids, onIdClick }: { ids: string[]; onIdClick: (id: string) => void }) {
  if (ids.length === 0) {
    return <span className="text-muted">{i18nT('pages.chat.decisionStrip.memory_none')}</span>
  }
  return (
    <span className="flex flex-wrap items-center gap-1">
      {ids.map(id => (
        <button
          key={id}
          type="button"
          onClick={() => onIdClick(id)}
          className={idChipClass}
          title={id}
        >
          {clipId(id)}
        </button>
      ))}
    </span>
  )
}

/** One labelled measurement in the expanded body. */
function Detail({ label, value }: { label: string; value: string | React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5 min-w-0">
      <span className="shrink-0 opacity-75">{label}</span>
      {typeof value === 'string' ? (
        <span className="text-text tabular-nums truncate">{value}</span>
      ) : (
        <div className="flex-1 min-w-0">{value}</div>
      )}
    </div>
  )
}

/**
 * The transcript's receipt for one recalled-memory decision.
 *
 * The record on the row is the ONLY condition, for the reason `DecisionStrip`
 * states: a stamped record is history that already sits on this machine, so
 * drawing it sends nothing, while the Decisions (Jev) switch governs whether a
 * FUTURE turn may ask.
 *
 * Collapsed it is COUNTS, not names — how many memories similarity recalled, how
 * many Jev kept, how sure it was on average, how long it took, and the prompt
 * characters the narrower block saved. Counts rather than the two id lists the
 * skill strip prints, because a memory id is a store handle a reader cannot read
 * anything off: six of them on one line would be six opaque words where the only
 * actionable fact is "three were dropped". The ids are in the expanded body,
 * where a reader who wants to look one up can.
 *
 * Both counts are printed even when they are equal. `agree` is on the record and
 * is not a branch here: "similarity 6 · Jev kept 6" already says they agreed, and
 * a separate agreed line would be a second way to say one thing.
 *
 * Expansion survives the row being recycled out of the virtualised transcript
 * (`useRowDisclosure`); the thumbs survive it through their own store. Both
 * thumbs pairs are `DecisionVerdictThumbs`, shared with the skill strip and the
 * tool card, so what a press sends and what a failure looks like are one
 * implementation rather than three.
 */
const MemoryRecallStrip = memo(function MemoryRecallStrip({
  record,
  disclosureKey,
}: {
  record: MemoryRecallRecord
  disclosureKey?: string
}) {
  // memo() bails out of the provider-level repaint; subscribe so a language
  // switch repaints this row's strings.
  useLanguageGeneration()
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, false)
  const [selectedMemoryId, setSelectedMemoryId] = useState<string | null>(null)
  const panelId = useId()

  const latency = record.latencyMs > 0
    ? i18nT('pages.chat.decisionStrip.latency_value', { ms: fmtNumber(record.latencyMs) })
    : null
  // Both numbers describe the answer, so they share one parenthetical rather than
  // each taking a segment of a line that already truncates. Joined with `fmtList`
  // because the separator between two list items is a locale's decision.
  //
  // The probability carries its own WORD, reusing the steer line's `steer_confidence`
  // rather than declaring a second spelling of one string: a bare `0.81` beside a
  // latency reads as a second duration, and the hover legend that explains it is
  // unreachable on touch and silent to a screen reader. The latency already names its
  // unit for the same reason.
  const scores = [
    record.p !== null
      ? i18nT('pages.chat.decisionStrip.steer_confidence', { p: confidence(record.p) })
      : null,
    latency,
  ].filter((part): part is string => part !== null)
  // The legend names what the group actually holds, so all three cases get their
  // own sentence instead of one describing a number that is not there.
  const scoresTitle = record.p !== null && latency !== null
    ? i18nT('pages.chat.decisionStrip.memory_confidence_latency_title')
    : record.p !== null
      ? i18nT('pages.chat.decisionStrip.memory_confidence_title')
      : i18nT('pages.chat.decisionStrip.latency_title')

  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-muted mt-1"
      data-testid="memory-recall-strip"
      data-agree={record.agree}
      data-expanded={expanded}
      // Beside `data-agree` and for the same reason: the screenshot harness selects a
      // row by the state it is photographing, and "the payload budget dropped some of
      // what Jev kept" is not inferable from the two id lists.
      data-bounded={record.boundedOmitted}
    >
      <div className="flex items-center gap-1.5 px-2 py-1 min-w-0 text-[12px] leading-5">
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          aria-controls={panelId}
          // No aria-label: the line's own text names the button, and
          // aria-expanded carries the state — same as DecisionStrip's toggle.
          className="flex-1 flex items-center gap-1.5 min-w-0 text-left hover:text-text transition-colors"
          data-testid="memory-recall-strip-toggle"
        >
          <ChevronRight
            className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
            aria-hidden="true"
          />
          <Brain className="lucide-inline shrink-0" aria-hidden="true" />
          {/* Titled with its SCOPE, because one turn can hold more than one receipt
              and only one is drawn. An agent may call `memory_recall` twice in a
              turn; each call is its own request with its own decision, and the
              hand-off registry keeps one entry per point, so the newer publish
              replaces the older. The line is about the LATEST recall of the turn,
              and the panel below says so in words -- a bare "memory" would read as
              a receipt covering every recall the turn made. */}
          <span
            className="shrink-0 font-medium text-text"
            title={i18nT('pages.chat.decisionStrip.memory_latest_title')}
          >
            {i18nT('pages.chat.decisionStrip.point_memory_recall')}{' \u00B7'}
          </span>
          <span className="truncate min-w-0 tabular-nums">
            {i18nT('pages.chat.decisionStrip.memory_similarity', {
              count: fmtNumber(record.baselineKeys.length),
            })}
            {' \u00B7 '}
            {/* On a FAILED record the second count is not Jev's. The decision did not
                land, the shipped recall was injected, and a line reading "Jev kept: 6"
                beside "Decision failed" credits an actor that chose nothing. */}
            {i18nT(
              record.error
                ? 'pages.chat.decisionStrip.memory_kept_fallback'
                : 'pages.chat.decisionStrip.memory_kept',
              { count: fmtNumber(record.jevKeys.length) },
            )}
          </span>
          {scores.length > 0 && (
            <span
              className="shrink-0 tabular-nums"
              title={scoresTitle}
              data-testid="memory-recall-strip-scores"
            >
              ({fmtList(scores, { type: 'unit' })})
            </span>
          )}
          {record.charsSaved > 0 && (
            <span
              className="shrink-0 tabular-nums"
              title={i18nT('pages.chat.decisionStrip.memory_saved_title')}
              data-testid="memory-recall-strip-saved"
            >
              {'\u00B7 '}
              {i18nT('pages.chat.decisionStrip.memory_saved_chars', {
                chars: fmtCompact(record.charsSaved),
              })}
            </span>
          )}
        </button>
        {/* Its own key, not the skills strip's shared `rate_jev`: both pairs here rate
            a MEMORY pick, so each label has to say it IS a rating -- an actor's name
            alone leaves a reader guessing what the thumbs do. Editing the shared key
            would change the skills strip and the tool-risk badge, which rate something
            else.

            Drawn only for a record that HAS a Jev pick. On a failure the line already
            says the decision did not land and the shipped recall was injected, so
            "Rate Jev's pick" beside "Decision failed" asks a reader to judge a choice
            nobody made -- and a thumb sent on it would be filed as a verdict on the
            judge. The similarity pair in the panel below is unaffected: that arm ran
            whatever the judge did. */}
        {!record.error && (
          <VerdictThumbs
            turnId={record.turnId}
            side="jev"
            label={i18nT('pages.chat.decisionStrip.memory_rate_jev')}
            rightLabel={i18nT('pages.chat.decisionStrip.memory_rate_right_jev')}
            wrongLabel={i18nT('pages.chat.decisionStrip.memory_rate_wrong_jev')}
          />
        )}
      </div>
      {expanded && (
        <div id={panelId} className="px-2 pb-2 pt-0 text-[12px] leading-5 flex flex-col gap-1 min-w-0">
          {/* The ids the collapsed line only counted. This is the one place a
              reader can check WHICH memory was dropped rather than that some
              number of them were. */}
          <Detail
            label={i18nT('pages.chat.decisionStrip.memory_baseline_label')}
            value={<IdList ids={record.baselineKeys} onIdClick={setSelectedMemoryId} />}
          />
          {/* Retitled rather than hidden on a failure: the list is still what the
              prompt carried, which is worth seeing -- it is the ATTRIBUTION that was
              wrong, not the content. */}
          <Detail
            label={i18nT(
              record.error
                ? 'pages.chat.decisionStrip.memory_jev_label_fallback'
                : 'pages.chat.decisionStrip.memory_jev_label',
            )}
            value={<IdList ids={record.jevKeys} onIdClick={setSelectedMemoryId} />}
          />
          <Detail
            label={i18nT('pages.chat.decisionStrip.memory_candidates_label')}
            value={fmtNumber(record.candidates)}
          />
          {/* Drawn only for a record that states the excerpt length, the same way
              the latency row below is: "message 0 chars" over a question that
              carried one is a false receipt. There is no history half — the
              memories ARE the prior conversation this question sends, so the
              excerpt is the whole of the egress beside them. */}
          {record.messageChars !== null && (
            <Detail
              label={i18nT('pages.chat.decisionStrip.sent_label')}
              value={i18nT('pages.chat.decisionStrip.sent_message', {
                chars: fmtNumber(record.messageChars),
              })}
            />
          )}
          {latency !== null && (
            <Detail label={i18nT('pages.chat.decisionStrip.latency_label')} value={latency} />
          )}
          {/* Drawn only when the response budget removed some of what Jev kept. The
              header's count is the DECISION's, so this row carries both numbers and
              the subtraction closes in one sentence: a reader who saw "Jev kept: 1"
              over "2 that Jev kept did not fit" could not reconcile them. Absent at
              0, because a row about a thing that did not happen is noise on every
              ordinary receipt. */}
          {record.boundedOmitted > 0 && (
            <Detail
              label={i18nT('pages.chat.decisionStrip.memory_bounded_label')}
              value={i18nT('pages.chat.decisionStrip.memory_bounded_value', {
                count: fmtNumber(record.boundedOmitted),
                total: fmtNumber(record.jevKeys.length),
              })}
            />
          )}
          {/* Hand-off ON: this strip holds no draft input, the host composer's
              draft is persisted per slot, and the failure category here is one an
              agent can actually act on — a timeout or a provider error names the
              judge, not the reply. An in-chat hand-off opens a fresh slot without
              navigating away, so there is nothing to lose. */}
          <ErrorNotice
            message={record.error}
            title={i18nT('pages.chat.decisionStrip.error_title')}
            variant="inline"
            askAgent
            testId="memory-recall-strip-error"
          />
          {/* The scope, in words rather than only in the header's tooltip: a reader
              deciding whether this receipt covers their whole turn cannot hover. */}
          <p className="m-0 text-muted" data-testid="memory-recall-strip-scope">
            {i18nT('pages.chat.decisionStrip.memory_latest_note')}
          </p>
          {/* On a FAILED record this pair is the only one left, sitting under "Decision
              failed" -- which reads as an invitation to rate something that did not
              happen. It did: the search ran and returned, and what failed was the
              narrowing on top of it. Said in words, because the thumbs are beside the
              failure notice and a reader has to know which of the two they are
              judging. */}
          {record.error !== null && (
            <p className="m-0 text-muted" data-testid="memory-recall-strip-search-ran">
              {i18nT('pages.chat.decisionStrip.memory_search_ran_note')}
            </p>
          )}
          <div className="flex items-center gap-1.5 min-w-0 pt-0.5">
            <VerdictThumbs
              turnId={record.turnId}
              side="baseline"
              label={i18nT('pages.chat.decisionStrip.memory_rate_baseline')}
              rightLabel={i18nT('pages.chat.decisionStrip.memory_rate_right_baseline')}
              wrongLabel={i18nT('pages.chat.decisionStrip.memory_rate_wrong_baseline')}
            />
          </div>
        </div>
      )}
      {/* Mounted only while an id is selected: the dialog runs a `useQuery`, so
          keeping it mounted when closed would demand a QueryClient of every host
          (and every test) that renders a strip with nothing open. */}
      {selectedMemoryId !== null && (
        <MemoryPopover
          recordId={selectedMemoryId}
          store={record.store}
          onClose={() => setSelectedMemoryId(null)}
        />
      )}
    </div>
  )
})

export default MemoryRecallStrip