/**
 * Which KAS run (and step) a chat's side panel shows, per chat slot.
 *
 * Shared between the in-chat run card, which sets it when clicked, and the
 * side panel's Workflows tab, which reads it to open that run's detail. Kept in
 * memory only: like the IDE's rail, a reload starts back at the run list.
 */
import { useCallback, useSyncExternalStore } from 'react'

export interface KasSelection {
  workflowId: string
  /** The step whose own chat is shown; absent = the default step. */
  nodeId?: string
  /** The step instance's runtime path, so one loop iteration can be selected
   *  apart from another that shares the same `nodeId`. */
  path?: string
}

const selections = new Map<string, KasSelection>()
const listeners = new Set<() => void>()

function emit() {
  for (const listener of listeners) listener()
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function getKasSelection(slotKey: string | null | undefined): KasSelection | null {
  return slotKey ? (selections.get(slotKey) ?? null) : null
}

/** Select a run (and optionally a step) for `slotKey`; `null` returns to the list. */
export function setKasSelection(slotKey: string | null | undefined, selection: KasSelection | null): void {
  if (!slotKey) return
  const prev = selections.get(slotKey)
  if (!selection) {
    if (!prev) return
    selections.delete(slotKey)
  } else {
    if (prev && prev.workflowId === selection.workflowId && prev.nodeId === selection.nodeId && prev.path === selection.path) return
    // A fresh object per change, so `useSyncExternalStore` sees it.
    selections.set(slotKey, {
      workflowId: selection.workflowId,
      ...(selection.nodeId ? { nodeId: selection.nodeId } : {}),
      ...(selection.path ? { path: selection.path } : {}),
    })
  }
  emit()
}

/** Test seam: forget every selection. */
export function resetKasSelections(): void {
  selections.clear()
  emit()
}

/** The selection for `slotKey`, and a setter bound to it. */
export function useKasSelection(
  slotKey: string | null | undefined,
): [KasSelection | null, (selection: KasSelection | null) => void] {
  const getSnapshot = useCallback(() => getKasSelection(slotKey), [slotKey])
  const selection = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const set = useCallback((next: KasSelection | null) => setKasSelection(slotKey, next), [slotKey])
  return [selection, set]
}
