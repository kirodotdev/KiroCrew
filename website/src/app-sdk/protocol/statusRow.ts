import type { ChatMessage } from '../../types'
import { isSystemNoticeRow } from '../../lib/systemNotice'
import { isWorkflowCompletionMessage } from '../../lib/workflowCompletion'
import { isSubagentCompletionMessage } from '../../lib/subagentCompletion'

/** True for a row the gateway wrote, not the agent: a system notice, a parsed workflow
 *  completion or a sub-agent completion envelope. A malformed envelope is drawn, so it is not status. */
export function isStatusRow(m: ChatMessage): boolean {
  return isSystemNoticeRow(m) || isWorkflowCompletionMessage(m) || isSubagentCompletionMessage(m)
}
