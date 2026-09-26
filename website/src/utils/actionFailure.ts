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
  /**
   * Which writes this is about (`mode:chat-a`), so that write's own later
   * success can take the banner down — and only that banner. Without it a
   * success would have to clear blindly, and a pin that lands would wipe a fork
   * failure the reader has not seen yet. Keyed per (action, slot), never per
   * set: a bulk revert names one key per session it reverted, so the first of
   * them whose write lands takes the banner down, whatever else landed with it.
   * Empty when the reporter named no key.
   */
  actionKeys: string[]
}

/** The fields that arrived after the positional four, so no call reads `undefined, undefined, key`. */
export type ActionFailureOptions = {
  actionKey?: string | readonly string[]
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
 */
let current: ActionFailure | null = null
const listeners = new Set<() => void>()

function emit(): void {
  for (const l of listeners) l()
}

function normalizeActionKeys(actionKey: ActionFailureOptions['actionKey']): string[] {
  if (actionKey === undefined) return []
  const keys = typeof actionKey === 'string' ? [actionKey] : actionKey
  return keys.filter(Boolean)
}

export function reportActionFailure(
  message: string, subject?: string, report?: ErrorReport, heading?: string, opts?: ActionFailureOptions,
): void {
  if (!message) return
  const nextSubject = subject || undefined
  const nextReport = report || undefined
  // A heading names the subject, so it goes with it: an unnamed session gets the
  // bare sentence, as it does on the shared lead.
  const nextHeading = nextSubject ? heading || undefined : undefined
  const nextActionKeys = normalizeActionKeys(opts?.actionKey)
  // `findReport` returns the journal's own entry, so equal detail yields the same
  // object and comparing `report` by reference is honest rather than vacuous.
  if (
    current !== null
    && current.message === message
    && current.subject === nextSubject
    && current.report === nextReport
    && current.heading === nextHeading
    && current.actionKeys.join(',') === nextActionKeys.join(',')
  ) return
  // The reader gets a translated sentence; raw transport text ("Failed to fetch")
  // belongs in the journal the report points at, never in the notice.
  current = {
    message,
    ...(nextSubject ? { subject: nextSubject } : {}),
    ...(nextReport ? { report: nextReport } : {}),
    ...(nextHeading ? { heading: nextHeading } : {}),
    actionKeys: nextActionKeys,
  }
  emit()
}

/**
 * Take the failure down. With `actionKey`, only a failure reported under that
 * key: a write's success calls this so the banner cannot outlive the state it
 * describes, and a success must not take down a banner about a DIFFERENT write
 * the reader has not read yet. A failure reported under several keys goes down
 * on any one of them. Bare, it is the dismiss button and drops whatever is
 * showing.
 */
export function clearActionFailure(actionKey?: string): void {
  if (current === null) return
  if (actionKey !== undefined && !current.actionKeys.includes(actionKey)) return
  current = null
  emit()
}

// The dismiss button's clear, as its own function: bound straight to an
// `onClick`, `clearActionFailure` would receive the click event as `actionKey`
// and match nothing.
function dismissActionFailure(): void {
  clearActionFailure()
}

export function __resetActionFailureForTests(): void {
  current = null
  listeners.clear()
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => listeners.delete(onChange)
}

function snapshot(): ActionFailure | null {
  return current
}

export function useActionFailure(): { failure: ActionFailure | null; clear: () => void } {
  const failure = useSyncExternalStore(subscribe, snapshot, snapshot)
  return { failure, clear: dismissActionFailure }
}
