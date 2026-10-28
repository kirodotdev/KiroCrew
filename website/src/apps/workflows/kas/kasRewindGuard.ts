/**
 * Editing an earlier message while a Kiro Workflows run is live (gap 3).
 *
 * Edit & resend (the chat's rewind) discards the chat's conversation with Kiro
 * Agent, and that conversation can be the one driving a run the chat started.
 * Like the Kiro IDE's checkpoint guard, the edit is refused while a run started
 * from that message or after it is still live (a paused run counts), and allowed
 * with a note once every such run has ended: editing does not undo what the run
 * did. The gateway refuses the same rewind (409 `rewind_workflow_running`); this
 * is the client half, so the control is disabled before anyone types.
 */
import type { KasRunRow } from '../../../api/client'
import { isTerminalRun } from './kasModel'

export type KasRewindGuard = 'none' | 'blocked' | 'ended'

/** What the guard reads of a run row. */
export type KasRewindRun = Pick<KasRunRow, 'slotKey' | 'createdAt' | 'status'>

function epoch(stamp: string | null | undefined): number | null {
  if (!stamp) return null
  const ms = Date.parse(stamp)
  return Number.isNaN(ms) ? null : ms
}

/**
 * The guard for editing the message sent at `messageTs` in chat `slotKey`.
 * A run whose start time (or the message's) cannot be read counts as started
 * after it, so an unknown never lets the edit through.
 */
export function kasRewindGuard(runs: readonly KasRewindRun[], slotKey: string | null | undefined, messageTs: string | null | undefined): KasRewindGuard {
  if (!slotKey) return 'none'
  const since = epoch(messageTs)
  let ended = false
  for (const run of runs) {
    if (run.slotKey !== slotKey) continue
    const created = epoch(run.createdAt)
    if (since !== null && created !== null && created < since) continue
    if (!isTerminalRun(run)) return 'blocked'
    ended = true
  }
  return ended ? 'ended' : 'none'
}
