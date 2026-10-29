/**
 * Start on a goal form whose draft save has not answered yet. A loop created
 * before that answer would arm a goal the shared store may then refuse as a
 * conflict, so Start sends the save at once and acts on its result.
 */
import { GOAL_DRAFT_SYNC_TIMEOUT_MS, type GoalDraft, type GoalDraftPersistResult } from './goalDrafts'

/** The longest Start waits: a save already in flight and the one Start sent,
 *  each under the request deadline, plus a margin. A longer queue ends as a
 *  failure, so Start never hangs. */
export const START_DRAFT_SAVE_WAIT_MS = 2 * GOAL_DRAFT_SYNC_TIMEOUT_MS + 1_000

/** `start` once the store took the draft; otherwise Start does not run. */
export type StartDraftSaveOutcome = 'start' | 'conflict' | 'failed'

/** Wait for the save Start depends on, bounded by `timeoutMs`. A conflict
 *  includes a newer record that superseded this save; a timeout is a failure. */
export async function awaitDraftSaveForStart(
  save: Promise<GoalDraftPersistResult>,
  timeoutMs: number = START_DRAFT_SAVE_WAIT_MS,
): Promise<StartDraftSaveOutcome> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<'timeout'>(resolve => { timer = setTimeout(() => resolve('timeout'), timeoutMs) })
  try {
    const result = await Promise.race([save, deadline])
    if (result === 'persisted' || result === 'retained') return 'start'
    return result === 'conflict' || result === 'superseded' ? 'conflict' : 'failed'
  } finally {
    clearTimeout(timer)
  }
}

/** Show the editor as `saving` while Start waits on `save`, then hand the state
 *  back: `idle` on a confirmed save, `failed` on a timeout. A conflict or failure
 *  the save itself showed stays. True only when Start may create the loop. */
export async function saveDraftBeforeStart<T extends string>(
  setState: (update: (state: T) => T) => void,
  save: () => Promise<GoalDraftPersistResult>,
): Promise<boolean> {
  setState(() => 'saving' as T)
  const outcome = await awaitDraftSaveForStart(save())
  setState(state => (state === 'saving' ? (outcome === 'start' ? 'idle' : 'failed') as T : state))
  return outcome === 'start'
}

/** The loop fields of a confirmed draft, so Start creates the text the store took. */
export function confirmedDraftFields(draft: GoalDraft | null): { message: string, idle_secs: number, max_cycles: number } | null {
  return draft ? { message: draft.message, idle_secs: draft.idleSecs, max_cycles: draft.maxCycles } : null
}
