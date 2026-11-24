/**
 * Where a press becomes an action. Every input of the XR page (mouse, keys,
 * a hand, a controller) ends here, and this is the ONLY place that decides
 * between three outcomes:
 *
 *   act     a plain target was pressed and released
 *   write   a guarded target was pressed, and `PressGuard` committed it, and
 *           the request on the card is still the one the press began on
 *   refused a guarded press did not count; the reason goes back as a hint
 *
 * A refused press never reaches `write`. That is the rule the scene used to
 * break, so it now has no way to call a write except through this class.
 */
import { PressGuard, type CancelReason, type PressOutcome } from './input'

export interface RouteTarget {
  id: string
  guarded?: boolean
  /** What exactly a guarded press would decide: the request and its payload. */
  version?: string
}

export type Refusal = Extract<PressOutcome, { refused: string }>['refused'] | 'changed'

/** The card showed another request by the time the press ended. */
const CHANGED: Refusal = 'changed'

export interface RouterHooks {
  act: (id: string) => void
  write: (id: string, version: string) => void
  refused: (id: string, why: Refusal) => void
}

export class PressRouter {
  readonly guard = new PressGuard()
  private versions = new Map<string, string>()
  private pressed: { source: string; target: RouteTarget; version: string } | null = null
  /** The last outcome in words, for the status line and the tests. */
  last = ''

  constructor(private readonly hooks: RouterHooks) {}

  /**
   * The guarded targets on show right now. One that is new, or whose request
   * changed, waits before it can be pressed; one that vanished or changed
   * under a held press cancels that press.
   */
  sync(targets: readonly RouteTarget[], now: number): void {
    const seen = new Set<string>()
    for (const t of targets) {
      if (!t.guarded) continue
      seen.add(t.id)
      const v = t.version ?? ''
      if (this.versions.get(t.id) === v) continue
      this.versions.set(t.id, v)
      this.guard.arm(t.id, now)
    }
    for (const id of [...this.versions.keys()]) {
      if (seen.has(id)) continue
      this.versions.delete(id)
      if (this.pressed?.target.id === id) this.guard.cancel('card-changed')
    }
  }

  hover(source: string, id: string, now: number): void { this.guard.hover(source, id, now) }

  down(source: string, target: RouteTarget | null, now: number): void {
    const other = this.pressed && this.pressed.source !== source
    this.guard.down(source, target, now)
    // A second source only cancels; the first press stays the one on record.
    if (other) return
    this.pressed = target ? { source, target, version: target.version ?? '' } : null
  }

  up(source: string, id: string, now: number, completed = true): PressOutcome {
    const p = this.pressed
    const out = this.guard.up(source, id, now, completed)
    if (!p || p.source !== source) { this.last = 'refused ' + ('refused' in out ? out.refused : 'no-press'); return out }
    this.pressed = null
    if ('commit' in out) {
      if (!p.target.guarded) {
        this.last = `act ${out.commit}`
        this.hooks.act(out.commit)
      } else if (this.versions.get(p.target.id) !== p.version) {
        // The card now shows another request than the one that was pressed.
        this.last = `refused ${CHANGED}`
        this.hooks.refused(p.target.id, CHANGED)
        return { refused: 'spent' }
      } else {
        this.last = `write ${out.commit}`
        this.hooks.write(out.commit, p.version)
      }
      return out
    }
    this.last = `refused ${out.refused}`
    if (p.target.guarded) this.hooks.refused(p.target.id, out.refused)
    return out
  }

  cancel(reason: CancelReason): void { this.guard.cancel(reason) }

  /** A source let go of its press on purpose, without ending it on anything. There is no outcome, and nothing to say. */
  drop(source: string): void {
    if (this.pressed?.source !== source) return
    this.guard.down(source, null, 0)
    this.pressed = null
    this.last = 'dropped'
  }

  get pressedId(): string { return this.guard.pressed }
  holdProgress(now: number): number { return this.guard.holdProgress(now) }
}
