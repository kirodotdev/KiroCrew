/**
 * The press rules of the XR page, as a state machine with no clock and no DOM
 * of its own. Mouse, keyboard, an XR controller and a pinching hand all feed
 * it the same four events, so one set of tests covers every input.
 *
 * A plain target (open a Bot, close a panel) commits when the same source
 * presses and releases on it. A guarded target (Approve, Send) also needs the
 * aim to rest on it first, a hold, and a card that has been still for a
 * moment. A press that was cancelled is spent: only a new press can commit.
 */

export const AIM_REST_MS = 300
export const HOLD_MS = 800
export const DISARM_MS = 700

export type CancelReason =
  | 'blur' | 'hidden' | 'tracking-lost' | 'second-source' | 'left-target'
  | 'card-changed' | 'session-end' | 'system' | 'scrolled'

export interface PressTarget {
  id: string
  guarded?: boolean
}

interface Press {
  source: string
  target: PressTarget
  at: number
  spent: boolean
}

export type PressOutcome =
  | { commit: string }
  | { refused: 'no-press' | 'other-source' | 'other-target' | 'spent' | 'not-completed' | 'too-short' | 'disarmed' | 'no-aim' | 'puck' }

export class PressGuard {
  private press: Press | null = null
  private aim = new Map<string, { id: string; since: number }>()
  private armedAt = new Map<string, number>()
  /** Why the last press ended without a commit. Read by tests and the status line. */
  lastCancel: CancelReason | null = null

  /** A guarded target that just appeared, or whose card changed, waits before it can be pressed. */
  arm(targetId: string, now: number): void {
    this.armedAt.set(targetId, now)
    if (this.press?.target.id === targetId) this.cancel('card-changed')
  }

  /** Where a source aims right now ('' for nothing). */
  hover(source: string, targetId: string, now: number): void {
    const cur = this.aim.get(source)
    if (!cur || cur.id !== targetId) this.aim.set(source, { id: targetId, since: now })
    if (this.press && this.press.source === source && this.press.target.id !== targetId && this.press.target.guarded) {
      this.cancel('left-target')
    }
  }

  down(source: string, target: PressTarget | null, now: number): void {
    if (this.press && this.press.source !== source) {
      // A second hand or device pressing means the person is doing something else.
      this.cancel('second-source')
      return
    }
    if (!target) { this.press = null; return }
    const aim = this.aim.get(source)
    const rested = !!aim && aim.id === target.id && now - aim.since >= AIM_REST_MS
    this.press = { source, target, at: now, spent: false }
    this.lastCancel = null
    if (target.guarded && !rested) this.press.spent = true
  }

  /**
   * The release. `completed` is false when the system ended the press itself
   * (`selectend` with no `select`): that never commits.
   */
  up(source: string, targetId: string, now: number, completed = true): PressOutcome {
    const p = this.press
    if (!p) return { refused: 'no-press' }
    if (p.source !== source) return { refused: 'other-source' }
    this.press = null
    if (p.spent) return { refused: p.target.guarded && this.lastCancel === null ? 'no-aim' : 'spent' }
    if (!completed) return { refused: 'not-completed' }
    if (p.target.id !== targetId) return { refused: 'other-target' }
    if (p.target.guarded) {
      // The puck is a pointer without a hand behind it: it can aim, never commit a guarded press.
      if (source.startsWith('puck')) return { refused: 'puck' }
      const armed = this.armedAt.get(p.target.id)
      if (armed !== undefined && p.at - armed < DISARM_MS) return { refused: 'disarmed' }
      if (now - p.at < HOLD_MS) return { refused: 'too-short' }
    }
    return { commit: p.target.id }
  }

  /** Anything that means "the person is no longer doing this on purpose". */
  cancel(reason: CancelReason): void {
    this.lastCancel = reason
    if (this.press) this.press.spent = true
  }

  /** How far a guarded hold has come, 0..1, for the ring that shows it. */
  holdProgress(now: number): number {
    const p = this.press
    if (!p || !p.target.guarded || p.spent) return 0
    return Math.max(0, Math.min(1, (now - p.at) / HOLD_MS))
  }

  get pressed(): string { return this.press && !this.press.spent ? this.press.target.id : '' }
}
