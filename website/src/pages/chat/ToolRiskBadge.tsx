/**
 * The tool card's risk badge: one line saying Jev thought this call was worth a look.
 *
 * Drawn under the tool pill, on a row of its own, when the gateway stamped a
 * `tool.risk` record on this tool message. It is an ANNOTATION and says so: the
 * call was approved by the session's own permission policy without consulting
 * Jev, and the badge's `title` states that in words, because a coloured word on a
 * tool card would otherwise read as "this was blocked".
 *
 * Only `caution` and `risky` reach here — `toolRiskRecord.ts` refuses every other
 * tier — so the badge's presence IS the flag. A `safe` answer leaves the card
 * looking exactly as it does without the seam, which is what keeps the two
 * flagged tiers readable.
 *
 * The thumbs are `DecisionVerdictThumbs` with `side="jev"`, the same control and
 * the same `POST /api/decisions/feedback` route the skill-selection strip posts
 * to. One implementation, because the log's summary is keyed on that `side` and a
 * second copy would be a second chance to send the wrong one.
 */
import { memo } from 'react'
import { ShieldAlert } from 'lucide-react'

import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import VerdictThumbs from './DecisionVerdictThumbs'
import type { ToolRiskRecord } from './toolRiskRecord'

/** Two decimals, so `0.88` reads as a score and not as a rounded `0.9`. */
const confidence = (p: number) => fmtNumber(p, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

const ToolRiskBadge = memo(function ToolRiskBadge({ record }: { record: ToolRiskRecord }) {
  // memo() bails out of the provider-level repaint; subscribe so a language
  // switch repaints this row's strings.
  useLanguageGeneration()

  const tierWord = record.tier === 'risky'
    ? i18nT('pages.chat.toolRiskBadge.tier_risky')
    : i18nT('pages.chat.toolRiskBadge.tier_caution')
  // `text-warn` for both tiers, not `text-danger` for `risky`: the danger colour
  // is what this transcript uses for a call that was BLOCKED, and using it for an
  // annotation on a call that ran would say the opposite of what happened.
  return (
    <div
      className="ml-5 mt-1 flex items-center gap-1.5 min-w-0 text-[12px] leading-5 text-muted"
      data-testid="tool-risk-badge"
      data-tier={record.tier}
    >
      <ShieldAlert className="lucide-inline shrink-0 text-warn" aria-hidden="true" />
      <span
        className="shrink-0 text-warn"
        title={i18nT('pages.chat.toolRiskBadge.annotation_title')}
        data-testid="tool-risk-badge-tier"
      >
        {i18nT('pages.chat.toolRiskBadge.labelled_tier', { tier: tierWord })}
      </span>
      {record.p !== null && (
        <span
          className="shrink-0 tabular-nums"
          title={i18nT('pages.chat.toolRiskBadge.confidence_title')}
          data-testid="tool-risk-badge-confidence"
        >
          ({confidence(record.p)})
        </span>
      )}
      <VerdictThumbs
        turnId={record.turnId}
        side="jev"
        label={i18nT('pages.chat.toolRiskBadge.rate_jev')}
        rightLabel={i18nT('pages.chat.toolRiskBadge.rate_right_jev')}
        wrongLabel={i18nT('pages.chat.toolRiskBadge.rate_wrong_jev')}
        testIdStem="tool-risk-badge"
      />
    </div>
  )
})

export default ToolRiskBadge
