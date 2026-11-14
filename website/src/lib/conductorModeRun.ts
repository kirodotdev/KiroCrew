/** A conductor mode run's state on the Board and the Sessions row.
 *
 *  The gateway sends the part only it holds as `run` (`dashboard/conductor_mode_run.py`):
 *  `settled` is `done` (items, every one closed, no patrol loop on) or `stopped`
 *  (items open, no patrol loop on), else `''`. The live part (Pause all, what waits on
 *  the person, a turn running) is the dashboard's and comes first. */
import type { ConductorModeRun } from '../types'

export type ConductorModeRunState = 'paused' | 'waiting' | 'working' | 'done' | 'stopped'

/** The one state a surface draws, in a fixed order: Paused, Waiting for you,
 *  Working, Done, Stopped. `null` when there is nothing to call a run: no goal and
 *  no item. `working` is a turn or a milestone running; an unsettled board with
 *  items (its patrol still on) is working too. */
export function conductorModeRunState({ paused, needsYou, working, run, hasGoal, items }: {
  paused: boolean
  needsYou: boolean
  working: boolean
  run: ConductorModeRun | undefined | null
  hasGoal: boolean
  items: number
}): ConductorModeRunState | null {
  if (!hasGoal && items === 0) return null
  if (paused) return 'paused'
  if (needsYou) return 'waiting'
  if (working) return 'working'
  if (run?.settled === 'done' || run?.settled === 'stopped') return run.settled
  return items > 0 ? 'working' : null
}
