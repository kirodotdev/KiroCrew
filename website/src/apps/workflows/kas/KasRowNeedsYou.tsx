/**
 * The sidebar row's "needs you" line for Kiro Workflows (`SessionRow`'s
 * `workflow_needs_you` status, gap 8): a Kiro Agent workflow in this chat is
 * parked on a person (a step approval / question, or a non-user park). The row
 * ranks it ABOVE needs_input so the workflow-specific label wins when a workflow
 * is the reason; a bare ask_question hold falls through to needs_input. Warn
 * ink, static glyph: nothing is running. `slot_projection` also folds this hold
 * into `s.needs_input` server-side; this fires from the client poll too. Kept
 * here, with the rest of KAS's code, so `SessionRow.tsx` stays under the line cap.
 */
import { Workflow } from 'lucide-react'

import { i18nT } from '../../../i18n/t'

export function KasRowNeedsYou({ count, name, className, iconPx }: {
  /** How many of the chat's workflows are live; more than one names the count. */
  count: number
  name: string
  /** The row's status-line class and icon size, so the line matches its siblings. */
  className: string
  iconPx: number
}) {
  const label = count > 1
    ? i18nT('pages.chatSidebar.workflows_need_you', { count })
    : i18nT('pages.chatSidebar.workflow_needs_you', { name })
  return (
    <div className={className} title={label} data-testid="row-workflow-needs-you">
      <Workflow size={iconPx} className="shrink-0 text-warn" aria-hidden />
      <span className="truncate font-medium text-warn">{label}</span>
    </div>
  )
}
