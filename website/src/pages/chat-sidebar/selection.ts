/** Picking several session rows for one batch action (`./SelectionBar`): which rows
 *  are picked, and the presses that pick them. Shift-click picks the rows from the
 *  anchor to the pressed row; while any row is picked, a plain click, Enter or Space
 *  toggles the pressed row instead of opening it, and Escape drops the selection. The
 *  row menu's Select entry toggles one row through `RowSelectionContext`.
 *
 *  Cmd/Ctrl-click is left alone: it opens a background tab on every sidebar control
 *  (docs/decisions/2026-10-17-sidebar-create-controls-shift-opens-the-active-tab.md). */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { RowSelection } from '../../hooks/useRowSelection'
import { isPeerRow, sessionRowIdentity } from './rowIdentity'
import { opensElsewhereOf } from './rows'
import type { Slot } from './types'

const NONE: ReadonlySet<string> = new Set()

/** What a press on a session row does to the selection, or null when the press is
 *  the row's own. */
export function selectionGesture(
  e: { shiftKey: boolean; metaKey: boolean; ctrlKey: boolean; altKey: boolean },
  selecting: boolean,
): 'range' | 'toggle' | null {
  if (e.metaKey || e.ctrlKey || e.altKey) return null
  if (e.shiftKey) return 'range'
  return selecting ? 'toggle' : null
}

/** The rows from one row to another in a drawn order, both ends included; empty when
 *  either end is not drawn. */
export function rowsBetween(order: readonly string[], from: string, to: string): string[] {
  const a = order.indexOf(from)
  const b = order.indexOf(to)
  if (a < 0 || b < 0) return []
  return order.slice(Math.min(a, b), Math.max(a, b) + 1)
}

/** The session row a press landed on, unless it landed on a control inside the row
 *  (the row menu, fork, close, a chip link, the rename box), which keeps its press. */
function pressedRow(target: EventTarget | null, root: HTMLElement): HTMLElement | null {
  if (!(target instanceof Element) || !root.contains(target)) return null
  const row = target.closest<HTMLElement>('[data-session-row]')
  if (!row) return null
  const control = target.closest('button, a, input, textarea, select')
  return control && row.contains(control) ? null : row
}

/** The rows a person can see and press, in the order the lanes draw them. A row in
 *  a collapsed folder stays mounted under `[inert]` and is left out, as it is from
 *  the digit jump (`./shortcuts`). */
function drawnRows(root: HTMLElement): string[] {
  const order: string[] = []
  for (const el of root.querySelectorAll<HTMLElement>('[data-session-row]')) {
    const identity = el.dataset.sessionRow
    if (identity && !el.closest('[inert]') && !order.includes(identity)) order.push(identity)
  }
  return order
}

/** The picked rows, the presses that pick them and the row menu's entry. Only a row
 *  this sidebar owns, that the filters let through, can be picked (`rows`), and a
 *  picked row stays picked only while it is drawn: one that closed, that a search or
 *  a filter took off the list, or whose folder was collapsed leaves the selection,
 *  so a batch action never reaches a row the list is not showing. */
export function useSessionSelection({ rows, activeSlot, rootRef, dragging }: {
  rows: Slot[]
  activeSlot: string | null
  rootRef: React.RefObject<HTMLElement | null>
  /** A row drag is in flight: its Escape cancels the drag, not the selection. */
  dragging: boolean
}) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(NONE)
  /** Where a Shift-click range starts, and the selection it adds to. */
  const anchor = useRef<{ key: string; base: ReadonlySet<string> } | null>(null)
  /** The row whose click was just taken as a toggle: the second click of a double
   *  click on it is taken too, so it neither toggles back nor opens the row. */
  const taken = useRef<string | null>(null)
  const keyByIdentity = useMemo(() => {
    const map = new Map<string, string>()
    for (const slot of rows) {
      if (!isPeerRow(slot) && opensElsewhereOf(slot) === null) map.set(sessionRowIdentity(slot), slot.key)
    }
    return map
  }, [rows])
  const selecting = selected.size > 0
  /** The pickable rows drawn now, as local keys in drawn order. */
  const drawnKeys = useCallback((root: HTMLElement): string[] => {
    const keys: string[] = []
    for (const identity of drawnRows(root)) {
      const key = keyByIdentity.get(identity)
      if (key !== undefined) keys.push(key)
    }
    return keys
  }, [keyByIdentity])

  // After every commit, since what is drawn is read back from the DOM: a folder
  // collapse or a lane switch changes it without changing `rows`.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- run-after-every-commit is the point
  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root || selected.size === 0) return
    const drawn = new Set(drawnKeys(root))
    const live = [...selected].filter(key => drawn.has(key))
    if (live.length === selected.size) return
    if (live.length === 0) anchor.current = null
    setSelected(live.length > 0 ? new Set(live) : NONE)
  })
  // The bar leaves with the selection, and with it whatever held focus inside it
  // (its ✕, or the menu trigger a closing menu hands focus back to).
  const wasSelecting = useRef(false)
  useEffect(() => {
    const ended = wasSelecting.current && !selecting
    wasSelecting.current = selecting
    const root = rootRef.current
    if (!ended || !root || (document.activeElement !== null && document.activeElement !== document.body)) return
    const rowsDrawn = Array.from(root.querySelectorAll<HTMLElement>('[data-session-row]')).filter(el => !el.closest('[inert]'))
    ;(rowsDrawn.find(el => el.getAttribute('aria-current') === 'true') ?? rowsDrawn[0])?.focus()
  }, [selecting, rootRef])

  const clear = useCallback(() => {
    anchor.current = null
    setSelected(NONE)
  }, [])
  const toggle = useCallback((key: string) => {
    const next = new Set(selected)
    if (!next.delete(key)) next.add(key)
    anchor.current = next.size > 0 ? { key, base: next } : null
    setSelected(next.size > 0 ? next : NONE)
  }, [selected])
  /** Pick every row from the anchor to `key` in the order the lanes draw them. With
   *  nothing picked yet the range starts at the open session, as a file list's does. */
  const extendTo = useCallback((key: string, root: HTMLElement) => {
    const order = drawnKeys(root)
    const held = selecting ? anchor.current : null
    const start = held && order.includes(held.key)
      ? held
      : { key: !selecting && activeSlot !== null && order.includes(activeSlot) ? activeSlot : key, base: selected }
    anchor.current = start
    setSelected(new Set([...start.base, ...rowsBetween(order, start.key, key), key]))
  }, [drawnKeys, selecting, selected, activeSlot])

  const onClickCapture = useCallback((e: React.MouseEvent<HTMLElement>) => {
    const row = pressedRow(e.target, e.currentTarget)
    const identity = row?.dataset.sessionRow ?? ''
    if (e.detail > 1 && taken.current !== null && taken.current === identity) {
      e.preventDefault()
      e.stopPropagation()
      return
    }
    taken.current = null
    const gesture = selectionGesture(e, selecting)
    const key = gesture ? keyByIdentity.get(identity) : undefined
    if (!gesture || key === undefined) return
    e.preventDefault()
    e.stopPropagation()
    taken.current = identity
    if (gesture === 'toggle') toggle(key)
    else extendTo(key, e.currentTarget)
  }, [selecting, keyByIdentity, toggle, extendTo])
  // No rename opens from a double click the selection took, or over a selection.
  const onDoubleClickCapture = useCallback((e: React.MouseEvent<HTMLElement>) => {
    const row = pressedRow(e.target, e.currentTarget)
    if (row && (selecting || taken.current === row.dataset.sessionRow)) e.stopPropagation()
  }, [selecting])
  const onKeyDownCapture = useCallback((e: React.KeyboardEvent<HTMLElement>) => {
    if (!selecting || (e.key !== 'Enter' && e.key !== ' ')) return
    if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return
    // Only a key pressed on the row itself: an inner control keeps its own.
    const row = e.target instanceof HTMLElement && e.currentTarget.contains(e.target) ? e.target : null
    const key = row ? keyByIdentity.get(row.dataset.sessionRow ?? '') : undefined
    if (key === undefined) return
    e.preventDefault()
    e.stopPropagation()
    // A held key toggles once.
    if (!e.repeat) toggle(key)
  }, [selecting, keyByIdentity, toggle])
  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLElement>) => {
    if (!selecting || dragging || e.key !== 'Escape' || e.defaultPrevented) return
    // A menu, a dialog (both portalled out of the sidebar) and a text box keep
    // their own Escape.
    if (!(e.target instanceof Element) || !e.currentTarget.contains(e.target)) return
    if (e.target.closest('input, textarea, select')) return
    e.stopPropagation()
    clear()
  }, [selecting, dragging, clear])

  const menu = useMemo<RowSelection>(() => ({ has: key => selected.has(key), toggle }), [selected, toggle])
  /** Every row that can be picked and whether it is, for the row views; null at rest. */
  const marks = useMemo<ReadonlyMap<string, boolean> | null>(
    () => (selecting ? new Map([...keyByIdentity.values()].map(key => [key, selected.has(key)])) : null),
    [selecting, selected, keyByIdentity],
  )
  return {
    selected, selecting, marks, clear, menu,
    rootProps: { onClickCapture, onDoubleClickCapture, onKeyDownCapture, onKeyDown },
  }
}
