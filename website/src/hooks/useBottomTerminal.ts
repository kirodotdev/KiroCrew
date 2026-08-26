import { useSyncExternalStore } from 'react'
import { safeSetItem } from '../utils/safeStorage'

/* ── App-wide bottom terminal panel ───────────────────────────────────────
 * A single docked terminal panel shared by the ENTIRE app (every route), as
 * opposed to the chat-scoped activity-bar terminal tabs (usePanelTabs). It is
 * a TAB view — terminals only — mirroring the activity-bar tab strip: each tab
 * is its own PTY session, the active one is shown and the rest kept mounted
 * (hidden) so their shells survive tab switches.
 *
 * State is MODULE-LEVEL + localStorage-persisted (mirroring usePanelTabs and
 * terminalRegistry) rather than redux, so the panel survives route changes and
 * full reloads; on reload each persisted tab reconnects to its still-live PTY
 * (backend orphan-reaper window), the same way activity-bar terminal tabs do.
 * Tab session ids are persisted; the running shell is not. */

export interface TermTab {
  /** PTY session id — one live shell per tab. */
  id: string
  /** Working directory the shell spawned in (undefined = server default). */
  cwd?: string
}

/** Where the terminal panel is docked — like VS Code's Panel position. */
export type TerminalPosition = 'bottom' | 'right'

interface BottomTerminalState {
  open: boolean
  /** Panel height in px (resizable via the top grip, used when position = 'bottom'). */
  height: number
  /** Panel width in px (resizable via the left grip, used when position = 'right'). */
  width: number
  /** Docking position of the terminal panel. */
  position: TerminalPosition
  /** Terminal tabs, left → right. */
  tabs: TermTab[]
  /** Active (visible) tab. */
  activeId: string | null
}

const STORAGE_KEY = 'mc-bottom-terminal'
/** Min panel height in px; the grip can't drag below this. */
export const MIN_HEIGHT = 120
/** Default panel height on first open. */
const DEFAULT_HEIGHT = 300
/** Min panel width in px; the grip can't drag below this (right position). */
export const MIN_WIDTH = 200
/** Default panel width when first docked right. */
const DEFAULT_WIDTH = 420
/** Max concurrent terminal tabs (each is a live PTY). */
export const MAX_TERMINALS = 8

/** Fraction of the viewport height the bottom-docked panel may occupy. */
export const MAX_VH = 0.72
/** Fraction of the viewport width the right-docked panel may occupy. */
export const MAX_VW = 0.55

const mintId = () => Math.random().toString(36).slice(2, 14)
const clampHeight = (h: number) => Math.max(MIN_HEIGHT, Math.round(h))
const clampWidth = (w: number) => Math.max(MIN_WIDTH, Math.round(w))

/** Clamp a persisted dimension against the CURRENT viewport so a width saved
 *  on a wide monitor (e.g. 55% of 2560px = 1408px) doesn't overflow a narrow
 *  one. Applied at render time, not only during drag. */
export function clampToViewport(dim: number, axis: 'width' | 'height'): number {
  if (typeof window === 'undefined') return dim
  const max = axis === 'width'
    ? Math.round(window.innerWidth * MAX_VW)
    : Math.round(window.innerHeight * MAX_VH)
  return Math.min(max, Math.max(axis === 'width' ? MIN_WIDTH : MIN_HEIGHT, dim))
}

function loadPersisted(): BottomTerminalState {
  const base: BottomTerminalState = { open: false, height: DEFAULT_HEIGHT, width: DEFAULT_WIDTH, position: 'bottom', tabs: [], activeId: null }
  if (typeof localStorage === 'undefined') return base
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return base
    // `splits` is read as a fallback for the pre-tab-model dev shape.
    const p = JSON.parse(raw) as (Partial<BottomTerminalState> & { splits?: TermTab[] }) | null
    if (!p || typeof p !== 'object') return base
    const rawTabs = Array.isArray(p.tabs) ? p.tabs : Array.isArray(p.splits) ? p.splits : []
    const tabs = (rawTabs.filter(t => t && typeof (t as TermTab).id === 'string') as TermTab[]).slice(0, MAX_TERMINALS)
    return {
      // Only restore "open" when there were tabs to restore — a stale open flag
      // with no tabs would render an empty panel on boot.
      open: p.open === true && tabs.length > 0,
      height: typeof p.height === 'number' ? clampHeight(p.height) : DEFAULT_HEIGHT,
      width: typeof p.width === 'number' ? clampWidth(p.width) : DEFAULT_WIDTH,
      position: p.position === 'right' ? 'right' : 'bottom',
      tabs,
      activeId: tabs.some(t => t.id === p.activeId) ? (p.activeId as string) : (tabs[0]?.id ?? null),
    }
  } catch {
    return base
  }
}

let state: BottomTerminalState = loadPersisted()
const listeners = new Set<() => void>()

function emit() { for (const cb of listeners) cb() }

/* Cross-window sync: the terminal-popout window and the main dashboard share
 * this persisted store (one tab list, whichever window currently hosts the
 * panel). `storage` fires only in OTHER windows — never the writer — so
 * re-loading here can't loop; state is adopted without re-persisting. */
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.key !== STORAGE_KEY) return
    state = loadPersisted()
    emit()
  })

  /* Invalidate the viewport-clamp cache on window resize so the panel shrinks
   * when the browser window does — otherwise the panel stays oversized until
   * the next store mutation. Only emits when the clamped dimensions actually
   * change, so resize drags that don't shift the cap cause no re-render. */
  window.addEventListener('resize', () => {
    const h = clampToViewport(state.height, 'height')
    const w = clampToViewport(state.width, 'width')
    if (h === clampedState.height && w === clampedState.width) return
    clampedSource = null
    emit()
  })
}

function set(next: BottomTerminalState) {
  if (next === state) return
  state = next
  emit()
  try { safeSetItem(STORAGE_KEY, JSON.stringify(state)) } catch { /* quota / locked storage */ }
}

/* ── Actions (module functions so non-React callers — e.g. keyboard handlers,
 *    "Run in terminal" — can drive the panel too) ── */

/** Open the panel, minting a first terminal tab if it has none. */
export function openBottomTerminal(cwd?: string): void {
  const tabs = state.tabs.length ? state.tabs : [{ id: mintId(), cwd }]
  set({ ...state, open: true, tabs, activeId: state.activeId ?? tabs[0].id })
}

/** Hide the panel WITHOUT killing its shells — they stay warm for reopen. */
export function closeBottomTerminal(): void {
  if (!state.open) return
  set({ ...state, open: false })
}

export function toggleBottomTerminal(cwd?: string): void {
  if (state.open) closeBottomTerminal()
  else openBottomTerminal(cwd)
}

/** Open a new terminal tab instantly (mints a fresh PTY session). At the cap,
 *  focuses the last tab instead of spawning. Returns the new session id, or
 *  null at the cap. */
export function addTab(cwd?: string): string | null {
  if (state.tabs.length >= MAX_TERMINALS) {
    const last = state.tabs[state.tabs.length - 1]
    if (last) set({ ...state, open: true, activeId: last.id })
    return null
  }
  const id = mintId()
  set({ ...state, open: true, tabs: [...state.tabs, { id, cwd }], activeId: id })
  return id
}

/** Adopt an EXISTING terminal session as a bottom-panel tab (no new PTY) — used
 *  when moving a chat's terminal into the app-wide panel. The session id is
 *  preserved so its live shell + scrollback come along (the PTY/xterm live in
 *  terminalRegistry/termCache keyed by session id, independent of the view).
 *  Returns false at the cap so the caller leaves the tab in its source view. */
export function adoptTab(id: string, cwd?: string): boolean {
  if (state.tabs.some(t => t.id === id)) { set({ ...state, open: true, activeId: id }); return true }
  if (state.tabs.length >= MAX_TERMINALS) return false
  set({ ...state, open: true, tabs: [...state.tabs, { id, cwd }], activeId: id })
  return true
}

/** Remove a tab from the store (the caller disposes the PTY/xterm first).
 *  Closing the last tab also hides the panel. */
export function removeTab(id: string): void {
  const idx = state.tabs.findIndex(t => t.id === id)
  if (idx === -1) return
  const tabs = state.tabs.filter(t => t.id !== id)
  // Refocus a neighbor when closing the active tab (prefer the left one).
  const activeId = state.activeId !== id
    ? state.activeId
    : (tabs[idx - 1] ?? tabs[idx] ?? tabs[tabs.length - 1])?.id ?? null
  set({ ...state, tabs, activeId, open: tabs.length > 0 ? state.open : false })
}

export function setActiveTab(id: string): void {
  if (state.activeId === id) return
  set({ ...state, activeId: id })
}

/** Replace the tab order wholesale (drag-to-reorder in the strip). */
export function setTabsOrder(next: TermTab[]): void {
  set({ ...state, tabs: next })
}

export function setBottomTerminalHeight(px: number): void {
  const height = clampHeight(px)
  if (height === state.height) return
  set({ ...state, height })
}

export function setBottomTerminalWidth(px: number): void {
  const width = clampWidth(px)
  if (width === state.width) return
  set({ ...state, width })
}

/** Toggle the terminal panel between bottom and right docking. */
export function setTerminalPosition(position: TerminalPosition): void {
  if (position === state.position) return
  set({ ...state, position })
}

/** Toggle position (bottom ↔ right). */
export function toggleTerminalPosition(): void {
  setTerminalPosition(state.position === 'bottom' ? 'right' : 'bottom')
}

/* ── React binding ── */

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}
function getSnapshot(): BottomTerminalState { return state }

/** Cached viewport-clamped view of state. Rebuilt when the underlying state
 *  reference changes (via set()) or the resize listener invalidates the cache.
 *  useSyncExternalStore's Object.is check uses the cached reference to skip
 *  re-renders when nothing changed. */
let clampedState: BottomTerminalState = state
let clampedSource: BottomTerminalState | null = null
function getViewportClampedSnapshot(): BottomTerminalState {
  if (clampedSource !== state) {
    clampedSource = state
    clampedState = {
      ...state,
      height: clampToViewport(state.height, 'height'),
      width: clampToViewport(state.width, 'width'),
    }
  }
  return clampedState
}

export function useBottomTerminal(): BottomTerminalState {
  return useSyncExternalStore(subscribe, getViewportClampedSnapshot, getSnapshot)
}

/** Selector for just the `open` flag. App only needs this; returning a
 *  primitive lets useSyncExternalStore's Object.is check skip re-renders on
 *  unrelated state changes — notably setBottomTerminalHeight firing on every
 *  mousemove during a grip-drag. */
function getOpenSnapshot(): boolean { return state.open }
export function useBottomTerminalOpen(): boolean {
  return useSyncExternalStore(subscribe, getOpenSnapshot, getOpenSnapshot)
}

/** Selector for just the `position` field. */
function getPositionSnapshot(): TerminalPosition { return state.position }
export function useTerminalPosition(): TerminalPosition {
  return useSyncExternalStore(subscribe, getPositionSnapshot, getPositionSnapshot)
}

/** Test-only: reset the module store and its persisted copy. */
export function __resetBottomTerminal(): void {
  state = { open: false, height: DEFAULT_HEIGHT, width: DEFAULT_WIDTH, position: 'bottom', tabs: [], activeId: null }
  emit()
  if (typeof localStorage !== 'undefined') {
    try { localStorage.removeItem(STORAGE_KEY) } catch { /* ignore */ }
  }
}
