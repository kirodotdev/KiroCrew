import { useSyncExternalStore } from 'react'
import type { ErrorReport } from './errorReport'

export type ActionFailure = { message: string; subject?: string; report?: ErrorReport }

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

export function reportActionFailure(message: string, subject?: string, report?: ErrorReport): void {
  if (!message) return
  // The reader gets a translated sentence; raw transport text ("Failed to fetch")
  // belongs in the journal the report points at, never in the notice.
  current = { message, ...(subject ? { subject } : {}), ...(report ? { report } : {}) }
  emit()
}

export function clearActionFailure(): void {
  if (current === null) return
  current = null
  emit()
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
  return { failure, clear: clearActionFailure }
}
