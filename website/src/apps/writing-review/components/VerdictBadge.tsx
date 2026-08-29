// Red / yellow / green pill for the overall review verdict.
//
// Colours use design tokens (var(--danger), var(--warn), var(--ok)) so
// theme switches carry through; labels resolve through i18n so the pill
// is readable in any locale.
import type { Verdict } from '../lib/types'
import { i18nT } from '../../../i18n/t'

const VERDICT_CLASS: Record<Verdict, string> = {
  red: 'bg-danger-subtle text-danger border-danger',
  yellow: 'bg-warn-subtle text-warn border-warn',
  green: 'bg-ok-subtle text-ok border-ok',
}

const VERDICT_LABEL_KEY: Record<Verdict, string> = {
  red: 'apps.writingReview.verdictBadge.red',
  yellow: 'apps.writingReview.verdictBadge.yellow',
  green: 'apps.writingReview.verdictBadge.green',
}

export default function VerdictBadge({ verdict }: { verdict: Verdict }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full border text-[11px] font-medium uppercase tracking-wide ${VERDICT_CLASS[verdict]}`}
    >
      {i18nT(VERDICT_LABEL_KEY[verdict])}
    </span>
  )
}
