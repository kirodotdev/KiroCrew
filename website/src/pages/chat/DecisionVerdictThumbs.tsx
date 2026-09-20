/**
 * One reader's verdict on one Jev answer, as a thumbs pair.
 *
 * Shared by both surfaces that rate a decision — the skill-selection strip under
 * an assistant reply (`DecisionStrip.tsx`) and the risk badge on a tool card
 * (`ToolRiskBadge.tsx`) — so the two cannot drift on what a press sends, what it
 * remembers, or what a failure looks like. A second copy would be a second
 * chance to post the wrong `side`, and the log's summary is keyed on that field.
 *
 * Two thumbs, and they are ONE control: the verdict is single-choice
 * (`right` | `wrong` | none), which is the case `max-two-buttons-per-row` names
 * as not counting toward its cap. Pressing the lit thumb takes the answer back,
 * so a misclick is undoable without a third control.
 *
 * `label` is VISIBLE and required, and it lives here rather than at the call
 * sites so neither surface can ship without one. A bare thumb beside a line of
 * text does not say what it rates, and an `aria-label` answers that for a screen
 * reader only — `IconButton` renders no text and no tooltip of its own, so each
 * button also carries its meaning as a `title`.
 *
 * The answer is kept only after the server took it. An optimistic flip would
 * have to be rolled back on a failure, and there is an honest alternative: the
 * pair is disabled while the request is in flight, and a failure renders beside
 * it rather than silently reverting.
 *
 * The mutation is handed the SHARED query client explicitly rather than reading
 * one out of context, for the reason `app-sdk/appQuery.ts` gives: these rows are
 * drawn by `app-sdk/messageRenderers`, whose whole contract is that a host may
 * render a transcript outside the dashboard's React root.
 */
import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { ThumbsDown, ThumbsUp } from 'lucide-react'

import { api, type DecisionFeedbackSide, type DecisionVerdictValue } from '../../api/client'
import { queryClient } from '../../api/queryClient'
import ErrorNotice from '../../components/ErrorNotice'
import { IconButton } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { nextVerdict, recordedVerdict, rememberVerdict } from './decisionRecord'

export default function DecisionVerdictThumbs({
  turnId,
  side,
  label,
  rightLabel,
  wrongLabel,
  testIdStem = 'decision-strip',
}: {
  turnId: string
  side: DecisionFeedbackSide
  label: string
  rightLabel: string
  wrongLabel: string
  /** Prefix for this surface's testids, so a badge pair and a strip pair are
   *  addressable apart in a transcript that draws both. */
  testIdStem?: string
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
      <span className="shrink-0 opacity-75" data-testid={`${testIdStem}-rate-label-${side}`}>{label}</span>
      <IconButton
        aria-label={rightLabel}
        title={rightLabel}
        aria-pressed={verdict === 'right'}
        variant={verdict === 'right' ? 'active' : 'default'}
        disabled={mut.isPending}
        onClick={() => press('right')}
        data-testid={`${testIdStem}-right-${side}`}
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
        data-testid={`${testIdStem}-wrong-${side}`}
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
        testId={`${testIdStem}-feedback-error-${side}`}
      />
    </span>
  )
}
