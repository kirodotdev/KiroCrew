/** What a pending approval card of a chat whose sub-agents run inside the KAS engine
 *  shows beyond any other card, from the gateway's display-only fields
 *  (`dashboard/engine_subagent_cards.approval_card_fields`). Each draws nothing when
 *  its field is empty, so every other card looks as it did. Neither changes what
 *  a press decides. */
import { i18nT } from '../../i18n/t'

/** "Asked by sub-agent <name>": the sub-agent whose call the card is for, when the
 *  engine named it on the request. Drawn like `RaisedBy`, but as one inline box that
 *  wraps under the call's title when both do not fit on one line (a 320 px card). */
export function AskedBySubagent({ name }: { name: string }) {
  if (!name) return null
  return (
    <span role="note" className="inline-block max-w-full align-top break-words ml-1 rounded-full border border-border px-2 text-[12px] leading-5 text-muted" data-testid="asked-by-subagent">
      {i18nT('components.approvalSubagent.asked_by', { name })}
    </span>
  )
}

/** The task a sub-agent dispatch waiting on the card was given. Model-written: drawn
 *  as plain text, never as markup, and clamped. */
export function ApprovalSubagentTask({ task }: { task: string }) {
  if (!task) return null
  return (
    <p className="m-0 px-3.5 pt-2 text-[12px] leading-5 text-muted min-w-0 break-words whitespace-pre-wrap line-clamp-3" data-testid="approval-subagent-task">
      {i18nT('components.approvalSubagent.task_line', { task })}
    </p>
  )
}
