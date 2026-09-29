import { useSyncExternalStore } from 'react'
import type { ErrorReport } from './errorReport'

export type ActionFailure = {
  message: string
  subject?: string
  report?: ErrorReport
  /**
   * The bold lead, already naming `subject`. Without one the notice heads a named
   * subject with the shared "Couldn’t update", which is only true of a write that
   * changed something here — a copy sent elsewhere or a fork that created nothing
   * says what it failed to do instead.
   */
  heading?: string
}

/**
 * The in-page surface a rejected gateway write reports to.
 *
 * Four components rolled their optimistic state back and then said nothing, or
 * said it somewhere a reader cannot act on: a per-row tooltip inside a menu that
 * closes, or a native `alert()`. Both lose the detail and neither offers the
 * agent hand-off. This is one store rather than four handlers so a new write
 * surface inherits the reporting instead of re-deciding it — the same reason
 * `offlineProps` owns the refusal side.
 *
 * Module-level, not context: the reporters are mutation callbacks in hooks and
 * menu subtrees that unmount as the menu closes, so the failure has to outlive
 * the component that observed it.
 *
 * A stack, not one slot: a second rejection that landed before the reader looked
 * would otherwise overwrite the first, leaving that write silently undone, which
 * is the failure this store exists to prevent. The newest shows; dismissing it
 * brings back the one it covered. Bounded so a burst cannot grow it without limit;
 * past the bound the oldest goes, since the newest is the one the reader is
 * most likely acting on.
 */
export const MAX_PENDING_FAILURES = 10
let pending: ActionFailure[] = []
let current: ActionFailure | null = null
const listeners = new Set<() => void>()

function emit(): void {
  for (const l of listeners) l()
}

export function reportActionFailure(
  message: string, subject?: string, report?: ErrorReport, heading?: string,
): void {
  if (!message) return
  const nextSubject = subject || undefined
  const nextReport = report || undefined
  // A heading names the subject, so it goes with it: an unnamed session gets the
  // bare sentence, as it does on the shared lead.
  const nextHeading = nextSubject ? heading || undefined : undefined
  // `findReport` returns the journal's own entry, so equal detail yields the same
  // object and comparing `report` by reference is honest rather than vacuous.
  if (
    current !== null
    && current.message === message
    && current.subject === nextSubject
    && current.report === nextReport
    && current.heading === nextHeading
  ) return
  // The reader gets a translated sentence; raw transport text ("Failed to fetch")
  // belongs in the journal the report points at, never in the notice.
  current = {
    message,
    ...(nextSubject ? { subject: nextSubject } : {}),
    ...(nextReport ? { report: nextReport } : {}),
    ...(nextHeading ? { heading: nextHeading } : {}),
  }
  pending = [...pending, current].slice(-MAX_PENDING_FAILURES)
  emit()
}

/**
 * Take the shown failure down when the reader dismisses it, revealing the one it
 * covered, if any. The earlier entry is the same object it was, so a subscriber
 * sees the snapshot it already rendered once rather than a lookalike.
 */
export function clearActionFailure(): void {
  if (current === null) return
  pending = pending.slice(0, -1)
  current = pending[pending.length - 1] ?? null
  emit()
}

/**
 * Drops the state, not the subscribers: a test that resets after `render` still
 * has `ActionFailureNotice` mounted, and `useSyncExternalStore` subscribes once
 * (`subscribe` is module-stable), so clearing `listeners` would leave the next
 * `emit()` with nobody to reach. Unmounted components unsubscribe themselves.
 */
export function __resetActionFailureForTests(): void {
  pending = []
  current = null
  emit()
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => listeners.delete(onChange)
}

function snapshot(): ActionFailure | null {
  return current
}

// A number, so `useSyncExternalStore` compares it by value and a render reads a
// stable snapshot without a second object to keep identical.
function earlierSnapshot(): number {
  return Math.max(0, pending.length - 1)
}

export function useActionFailure(): { failure: ActionFailure | null; earlier: number; clear: () => void } {
  const failure = useSyncExternalStore(subscribe, snapshot, snapshot)
  const earlier = useSyncExternalStore(subscribe, earlierSnapshot, earlierSnapshot)
  return { failure, earlier, clear: clearActionFailure }
}
