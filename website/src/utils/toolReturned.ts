import type { ToolActivity } from '../types'

/** Whether a tool-log entry's call has returned. A call that returned while the
 *  tab did not watch its slot (`tool_status` with `returned: true`) is marked
 *  `output_partial === false` with no output: its output lives on the persisted
 *  row. Any other entry with output counts as returned, as before. */
export function toolReturned(e: Pick<ToolActivity, 'output' | 'output_partial'>): boolean {
  return e.output != null || e.output_partial === false
}
