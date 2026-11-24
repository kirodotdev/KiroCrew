/**
 * A Bot's plan, as beads: one ball per step, held up over the Bot. The steps
 * are the first numbered list of what the Bot wrote. Nothing is made up: a
 * reply with no list has no beads.
 *
 * shortcut: the Crew's data has no per-step progress today, so the beads of
 * a real reply are all filled when they show (the reply is the end of the
 * turn). Fill them one by one when a field for a step's state exists.
 */
export const BEADS_MAX = 5
/** On sample data, where the Bot is scripted: how long one step takes. */
export const BEAD_STEP_MS = 1100

/** The steps of the first numbered list in a text, in order, up to `BEADS_MAX`. Fewer than two is not a plan. */
export function planOf(text: string): string[] {
  const steps: string[] = []
  for (const line of text.split('\n')) {
    const m = /^\s*(\d{1,2})[.)]\s+(\S.*)$/.exec(line)
    if (m && Number(m[1]) === steps.length + 1) steps.push(m[2].trim())
    else if (steps.length) break
  }
  return steps.length >= 2 ? steps.slice(0, BEADS_MAX) : []
}

/** The part of a text before its first numbered list: the one line a Bot says with its plan. */
export function leadOf(text: string): string {
  const lines = text.split('\n')
  const at = lines.findIndex(l => /^\s*1[.)]\s+\S/.test(l))
  return (at < 0 ? lines : lines.slice(0, at)).join(' ').trim()
}

/** How many beads are filled `ms` after the work began, when the steps are scripted to take `stepMs` each (sample data only). */
export interface Bead { total: number; done: number }

export const beadsDone = (total: number, ms: number, stepMs: number): number => Math.max(0, Math.min(total, Math.floor(ms / stepMs)))
