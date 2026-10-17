/**
 * Switch All Sessions: which sessions a bulk model switch would change, and
 * why each of the others would not.
 *
 * The panel's "Switch N sessions" count is what is left after excluding
 * sessions. Every exclusion must be a NAMED bucket here, because each bucket
 * is what the panel renders to explain the gap between N and the rows the
 * user can see (#11103):
 *
 * - `onTarget`        -- already on the picked model; the backend reports
 *                        these as `unchanged`. Rendered as the
 *                        `bulk-model-on-target` line.
 * - `skippedRunning`  -- running and NOT on the pick, while "Skip running
 *                        sessions" is ticked. The checkbox label renders
 *                        `runningOffTarget`, which is this same set whether or
 *                        not the box is ticked, so no running session that is
 *                        already on the pick is counted twice on screen.
 *
 * The buckets are disjoint and, with `affected`, cover every slot, in the
 * server's own order (`api_chat_slots_model` checks the model before the busy
 * state, so a running slot already on the target is `onTarget`). A new reason
 * to exclude a session has to add a bucket here and a surface in the panel;
 * the partition test fails if one is filtered out without a bucket.
 */

export interface BulkSwitchSlot {
  key: string
  model?: string | null
  running?: boolean
}

export interface BulkSwitchPartition {
  affected: number
  onTarget: number
  skippedRunning: number
  /** Running sessions not on the pick: what the skip checkbox counts. */
  runningOffTarget: number
}

export function partitionBulkSwitch(
  slots: readonly BulkSwitchSlot[],
  pick: string,
  skipRunning: boolean,
): BulkSwitchPartition {
  const out: BulkSwitchPartition = { affected: 0, onTarget: 0, skippedRunning: 0, runningOffTarget: 0 }
  for (const s of slots) {
    if ((s.model ?? '') === pick) { out.onTarget++; continue }
    if (s.running) out.runningOffTarget++
    if (skipRunning && s.running) out.skippedRunning++
    else out.affected++
  }
  return out
}
