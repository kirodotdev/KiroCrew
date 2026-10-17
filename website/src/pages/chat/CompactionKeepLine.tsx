import { memo } from 'react'
import { Scissors } from 'lucide-react'

import { fmtNumber, fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import VerdictThumbs from './DecisionVerdictThumbs'
import type { CompactionKeepRecord } from './decisionRecord'

/**
 * The compaction card's receipt for one SHADOW scoring of that compaction.
 *
 * A compaction discards everything the harness does not summarize, and today's
 * recycle replay keeps the conversation text alone — every tool call and every tool
 * result is already gone. This line says what an oracle would have kept instead, and
 * it is the only place that number is visible: the compaction itself is byte-identical
 * whatever Jev answered, so without the line the measurement would exist only in a
 * log file.
 *
 * ONE line, not a disclosure card. Everything the record holds fits on it — how many
 * of the session's tool calls would have been kept, what share of the conversation's
 * characters that is, and (only on a session whose walk overflowed) how many calls
 * were never scored — and a collapsed card that hides nothing is a control that does
 * nothing (the same reasoning `SteerDecisionLine` states).
 *
 * The overflow qualifier sits INSIDE the count's own clause rather than beside the
 * character share, because it qualifies that count and nothing else: the share is
 * measured over the characters the arms did cover, so "(+K not scored)" trailing the
 * whole line would read as a caveat on both numbers.
 *
 * It says "would", not "did", and that wording is load-bearing rather than cautious:
 * nothing was kept. A reader who took this for a description of what the compaction
 * did would believe their tool history survived it.
 *
 * The character share says "up to" for a second reason of the same kind: the gateway's
 * numerator counts every conversation row the replay admits BY ROLE, and
 * `build_session_replay` then applies row quotas and a character budget on top, so a
 * long session is replayed with less. The number is an upper bound, so the copy states
 * a bound. Dropping the two words would make the line claim a measurement nobody took.
 *
 * The record on the row is the ONLY condition, the rule the other two receipts share:
 * a stamped record is history that already sits on this machine, so drawing it sends
 * nothing, and gating it on the current consent switch would lose the receipt for
 * every past compaction the moment the preview is turned off.
 *
 * The thumbs are `DecisionStrip`'s own pair, shared rather than copied. Side `jev`:
 * the record names one answer by one party, and the arm it is compared against is a
 * replay rule with no opinion to rate.
 */
const CompactionKeepLine = memo(function CompactionKeepLine({
  record,
}: {
  record: CompactionKeepRecord
}) {
  // memo() bails out of the provider-level repaint; subscribe so a language switch
  // repaints this row's strings.
  useLanguageGeneration()

  // Through the format seam, not a template literal: Latin digits are wrong for bn,
  // and a bare `${n}%` would follow the browser's locale rather than the app's.
  const kept = i18nT('pages.chat.compactionKeep.summary', {
    kept: fmtNumber(record.keptCalls),
    total: fmtNumber(record.totalCalls),
  })
  const share = record.charsShare === null
    ? null
    : i18nT('pages.chat.compactionKeep.chars_share', { percent: fmtPercent(record.charsShare) })
  // Drawn ONLY when the gateway's walk actually overflowed. On every ordinary session
  // this is 0 and the line is unchanged, which is what keeps the qualifier meaningful
  // where it does appear: a permanent "(+0 not scored)" is furniture.
  const truncated = record.truncatedCalls > 0
    ? i18nT('pages.chat.compactionKeep.not_scored', { count: fmtNumber(record.truncatedCalls) })
    : null

  return (
    <div
      className="flex items-center gap-1.5 px-3 pb-2 text-[12px] leading-5 text-muted min-w-0"
      data-testid="compaction-keep-line"
    >
      <Scissors size={12} className="shrink-0" aria-hidden="true" />
      <span className="truncate min-w-0">
        {[truncated === null ? kept : `${kept} ${truncated}`, share]
          .filter((part): part is string => part !== null)
          .join(' · ')}
      </span>
      <VerdictThumbs
        turnId={record.turnId}
        side="jev"
        // The label names the ACT, not Jev: the sentence beside it already opens with
        // Jev's name, and a second mention read as an unexplained repeat on the steer
        // line this pair is shared with.
        label={i18nT('pages.chat.compactionKeep.rate_label')}
        rightLabel={i18nT('pages.chat.compactionKeep.rate_right')}
        wrongLabel={i18nT('pages.chat.compactionKeep.rate_wrong')}
        testIdStem="compaction-keep"
      />
    </div>
  )
})

export default CompactionKeepLine
