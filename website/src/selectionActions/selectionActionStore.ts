/**
 * What the selection-action layer shows: nothing, the apps menu, or one item's popover.
 *
 * A selection toolbar writes here (`useAppSelectionActions`); the one mounted
 * `SelectionActionLayer` reads it. The menu holds no items: it reads the app list
 * itself each time it opens. Opening anything replaces what was open, so at
 * most one invocation is ever live.
 *
 * @module selectionActions/selectionActionStore
 */
import { useSyncExternalStore } from 'react'
import type { ResolvedSelectionAction } from './selectionActionRegistry'
import type { SelectionSourceKind } from './selectionSources'

export interface Anchor {
  left: number
  top: number
  bottom: number
}

export type SelectionLayerState =
  | { kind: 'none' }
  | { kind: 'menu'; seq: number; source: SelectionSourceKind; text: string; anchor: Anchor }
  | { kind: 'item'; seq: number; item: ResolvedSelectionAction; source: SelectionSourceKind; text: string; anchor: Anchor }

let state: SelectionLayerState = { kind: 'none' }
let seq = 0
const listeners = new Set<() => void>()

// Whether ANY enabled app has an approved selection action, published by the
// mounted layer from the dashboard's shared app list. It only decides whether the
// toolbar shows the Apps button; it never supplies the items the menu lists.
let available = false
const availableListeners = new Set<() => void>()

export function setSelectionActionsAvailable(next: boolean): void {
  if (next === available) return
  available = next
  availableListeners.forEach((l) => l())
}

export function useSelectionActionsAvailable(): boolean {
  return useSyncExternalStore(
    (l) => { availableListeners.add(l); return () => { availableListeners.delete(l) } },
    () => available,
    () => available,
  )
}

function set(next: SelectionLayerState): void {
  state = next
  listeners.forEach((l) => l())
}

const anchorOf = (rect: DOMRect): Anchor => ({ left: rect.left, top: rect.top, bottom: rect.bottom })

export function openSelectionMenu(source: SelectionSourceKind, text: string, rect: DOMRect): void {
  set({ kind: 'menu', seq: ++seq, source, text, anchor: anchorOf(rect) })
}

export function openSelectionItem(item: ResolvedSelectionAction, source: SelectionSourceKind, text: string, rect: DOMRect | Anchor): void {
  const anchor = rect instanceof DOMRect ? anchorOf(rect) : rect
  set({ kind: 'item', seq: ++seq, item, source, text, anchor })
}

export function closeSelectionLayer(): void {
  if (state.kind !== 'none') set({ kind: 'none' })
}

export function useSelectionLayerState(): SelectionLayerState {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => { listeners.delete(l) } },
    () => state,
    () => state,
  )
}
