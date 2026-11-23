import { createContext, useCallback, useContext, useLayoutEffect, useState, useSyncExternalStore } from 'react'
import { isPeerRow } from './rowIdentity'
import type { Slot } from './types'

/**
 * Which session the chat pane shows, and which sessions are unread, as one small
 * store the session rows subscribe to one answer at a time.
 *
 * Why a store and not row props: a chat switch changes these values for two rows
 * (the one that was open and the one that is now), but a prop reaches a row only
 * through every component above it, so the whole list re-rendered to repaint two
 * highlights. A row here selects its own boolean ("am I the open row", "am I
 * unread"), and React re-renders only the rows whose answer flipped.
 */
export interface RowStatus {
  /** The LOCAL slot the chat pane shows. */
  activeSlot: string | null
  /** The crew window open over the pane, naming a peer's own slot by (crew, key), or
   *  null. While one is open it is the active row, and no local row is. */
  crewWindow: { instanceId: string; key: string } | null
  /** The LOCAL Crew Mode root whose worker the chat pane shows, or null. Workers
   *  are not listed, so the root's row is marked as the shown worker's parent. */
  crewParent: string | null
  /** The chat page is in Split View. */
  splitMode: boolean
  /** The last open of the active chat failed and has not loaded since
   *  (`ChatState.slotOpenFailed`): a click on its row retries the open instead
   *  of toggling. */
  openFailed: boolean
  /** LOCAL slot keys with output the person has not read. */
  unread: ReadonlySet<string>
}

const NO_UNREAD: ReadonlySet<string> = new Set()
const EMPTY: RowStatus = { activeSlot: null, crewWindow: null, crewParent: null, splitMode: false, openFailed: false, unread: NO_UNREAD }

/** A row's LOCAL key, or null for a peer row, whose raw key can equal a local one. */
function localKey(slot: Slot): string | null {
  return isPeerRow(slot) ? null : slot.key
}

/** The active row. A peer row is active only for the crew window open on its own
 *  (crew, key) PAIR -- its raw key alone can equal another crew's, or a local slot's.
 *  A local row is active only while no crew window is open, because the window then
 *  covers the pane its slot would show. */
export function isActiveIn(status: Pick<RowStatus, 'activeSlot' | 'crewWindow'>, slot: Slot): boolean {
  const key = localKey(slot)
  const win = status.crewWindow
  if (key === null) return !!win && win.instanceId === slot.peer_id && win.key === slot.key
  return !win && status.activeSlot === key
}

/** What one row shows from the store. Every member is a boolean, so a row's
 *  selection only changes when its own answer does. */
export interface RowStatusView {
  isActive: boolean
  /** The raw `key === activeSlot` test, independent of the crew-window logic
   *  `isActive` folds in (a crew window over the pane makes `isActive` false while
   *  the session is still the active one). "Reference in chat" reads it to refuse
   *  a self-reference, matching what the drop handler refuses. */
  isSidebarActiveSlot: boolean
  /** A plain click on this row shows or hides the side panel: it is the active
   *  LOCAL row, outside Split View (lib/reclickPanelToggle). */
  reclickPanel: boolean
  /** This row is the Crew Mode root whose worker the chat pane shows. */
  isCrewParent: boolean
  isUnread: boolean
}

export function rowStatusOf(status: RowStatus, slot: Slot): RowStatusView {
  const local = localKey(slot)
  const isActive = isActiveIn(status, slot)
  return {
    isActive,
    isSidebarActiveSlot: local !== null && status.activeSlot === local,
    reclickPanel: local !== null && !status.splitMode && !status.openFailed && isActive,
    isCrewParent: local !== null && !status.crewWindow && status.crewParent === local,
    isUnread: local !== null && status.unread.has(local),
  }
}

export interface RowStatusStore {
  get(): RowStatus
  /** Replace the values; listeners hear of it on `publish`. */
  set(next: RowStatus): void
  publish(): void
  subscribe(listener: () => void): () => void
}

export function createRowStatusStore(initial: RowStatus = EMPTY): RowStatusStore {
  let state = initial
  let published = initial
  const listeners = new Set<() => void>()
  return {
    get: () => state,
    set(next) { state = next },
    publish() {
      if (published === state) return
      published = state
      for (const l of listeners) l()
    },
    subscribe(listener) { listeners.add(listener); return () => { listeners.delete(listener) } },
  }
}

function sameStatus(a: RowStatus, b: RowStatus): boolean {
  return a.activeSlot === b.activeSlot && a.crewParent === b.crewParent && a.splitMode === b.splitMode
    && a.openFailed === b.openFailed && a.unread === b.unread
    && (a.crewWindow === b.crewWindow || (!!a.crewWindow && !!b.crewWindow
      && a.crewWindow.instanceId === b.crewWindow.instanceId && a.crewWindow.key === b.crewWindow.key))
}

/** The store a list's rows read. Outside a provider (a row rendered on its own, as
 *  in tests) every row reads as inactive and read. */
export const RowStatusContext = createContext<RowStatusStore>(createRowStatusStore())

/** Own a store and keep it at `status`. The values are written during render,
 *  before the rows below read them, so a row that renders in this pass already
 *  sees them; the rows that skipped this pass hear of the change from the layout
 *  effect, before the frame paints. */
export function useRowStatusStore(status: RowStatus): RowStatusStore {
  const [store] = useState(() => createRowStatusStore(status))
  if (!sameStatus(store.get(), status)) store.set(status)
  useLayoutEffect(() => { store.publish() })
  return store
}

/** This row's answers from the nearest store. */
export function useRowStatus(slot: Slot): RowStatusView {
  const store = useContext(RowStatusContext)
  const getSnapshot = useCallback(() => packView(rowStatusOf(store.get(), slot)), [store, slot])
  return unpackView(useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot))
}

// One number per row, so the snapshot compares by value: bit 0 active, 1 sidebar
// active, 2 reclick, 3 crew parent, 4 unread.
function packView(v: RowStatusView): number {
  return (v.isActive ? 1 : 0) | (v.isSidebarActiveSlot ? 2 : 0) | (v.reclickPanel ? 4 : 0) | (v.isCrewParent ? 8 : 0) | (v.isUnread ? 16 : 0)
}
function unpackView(n: number): RowStatusView {
  return { isActive: !!(n & 1), isSidebarActiveSlot: !!(n & 2), reclickPanel: !!(n & 4), isCrewParent: !!(n & 8), isUnread: !!(n & 16) }
}
