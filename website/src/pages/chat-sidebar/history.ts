/** The Older Sessions pane: open state (including the `?history=1` arrival intent),
 *  its persisted height and its resize drag. */
import { useState, useCallback, useEffect, useRef, type Dispatch, type SetStateAction } from 'react'
import { fetchHistory } from '../../store/chatSlice'
import { safeSetItem } from '../../utils/safeStorage'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import type { AppDispatch } from '../../store'
import { HISTORY_HEIGHT_LS_KEY } from './persistence'

/** Older Sessions open state, persisted height and resize drag. */
export function useHistoryPane({ setHistoryFilter, slotFilter, dispatch }: {
  setHistoryFilter: Dispatch<SetStateAction<string>>
  slotFilter: string
  dispatch: AppDispatch
}) {
  // Opened on arrival when the URL asks for it (`/chat?history=1`), so a surface
  // that can only POINT at an archived transcript — Issue Radar's declined
  // re-investigate notice — can land the user on the pane holding it instead of
  // naming a pane they then have to find.
  //
  // Read from `window.location` rather than `useSearchParams` deliberately: this
  // component is rendered bare (no router) by a large number of its own tests, and
  // a router hook here would make every one of them a provider error. Read once,
  // in the initializer, because it is an ARRIVAL intent — re-reading it would
  // re-open a pane the user has since collapsed, and every route that carries the
  // param mounts this component fresh.
  const [historyOpen, setHistoryOpen] = useState(() => {
    try { return new URLSearchParams(window.location.search).get('history') === '1' }
    catch { return false }
  })
  // The main session search is the broad entry point. Carry it into Older
  // Sessions when that pane opens, then keep following it while both controls
  // are visible. The history field can still be refined independently: only a
  // later edit to the main search intentionally replaces that refinement.
  const openHistoryPane = useCallback(() => {
    setHistoryFilter(slotFilter)
    setHistoryOpen(true)
    dispatch(fetchHistory(false))
  }, [dispatch, slotFilter, setHistoryFilter])
  useEffect(() => {
    if (historyOpen) setHistoryFilter(slotFilter)
  }, [historyOpen, slotFilter, setHistoryFilter])
  // The pane's toggle fetches when it OPENS the pane, so a pane that starts open
  // has never fetched and would render its empty state over real history.
  useEffect(() => {
    if (historyOpen) dispatch(fetchHistory(false))
    // Arrival only — deliberately not re-run when the user toggles the pane, which
    // does its own fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  // History pane height (persisted). Drag handle adjusts this while open.
  const HISTORY_MIN_HEIGHT = 120
  const HISTORY_MAX_HEIGHT = 800
  const [historyHeight, setHistoryHeight] = useState<number>(() => {
    const saved = parseInt(localStorage.getItem(HISTORY_HEIGHT_LS_KEY) || '', 10)
    return Number.isFinite(saved) && saved >= HISTORY_MIN_HEIGHT && saved <= HISTORY_MAX_HEIGHT ? saved : 240
  })
  useEffect(() => { safeSetItem(HISTORY_HEIGHT_LS_KEY, String(historyHeight)) }, [historyHeight])
  const [historyDragging, setHistoryDragging] = useState(false)
  const historyStartHRef = useRef(0)
  const historyDraggingRef = useRef(false)
  const historyResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      historyStartHRef.current = historyHeight
      historyDraggingRef.current = true
      setHistoryDragging(true)
      document.body.style.cursor = 'ns-resize'
      document.body.style.userSelect = 'none'
    },
    onMove: ({ dy }) => {
      // Drag handle is ABOVE the pane, so dragging UP (dy < 0) grows the pane.
      setHistoryHeight(Math.max(HISTORY_MIN_HEIGHT, Math.min(HISTORY_MAX_HEIGHT, historyStartHRef.current - dy)))
    },
    onEnd: () => {
      historyDraggingRef.current = false
      setHistoryDragging(false)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    },
  })
  // Unmount guard: onEnd can't fire if the sidebar unmounts mid-drag
  // (setPointerCapture dies with the element), so restore the global body styles
  // here to avoid leaving the resize cursor / text-selection lock stuck.
  useEffect(() => () => {
    if (historyDraggingRef.current) {
      historyDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [])
  return { historyOpen, setHistoryOpen, openHistoryPane, historyHeight, historyDragging, historyResize }
}
