import { useSyncExternalStore } from 'react'
import { safeGetItem, safeRemoveItem, safeSetItem } from '../utils/safeStorage'
import { secureRandomId } from '../utils/secureId'

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
 * (backend orphan-reaper window), the same way activity-bar terminal tabs do —
 * once the backend has confirmed which of those PTYs still exist (see the
 * hydrate-time reconciliation below). Tab session ids are persisted; the
 * running shell is not. */

export interface TermTab {
  /** PTY session id — one live shell per tab. */
  id: string
  /** Working directory the shell spawned in (undefined = server default). */
  cwd?: string
  /** User label; absent means follow the shell's live title. */
  name?: string
}

export const MAX_TERMINAL_NAME_LENGTH = 120

/** Cap labels and editing drafts by Unicode code points, preserving surrogate pairs. */
export function capTerminalName(name: string): string {
  return Array.from(name).slice(0, MAX_TERMINAL_NAME_LENGTH).join('')
}

function normalizeTabName(name: unknown): string | undefined {
  return typeof name === 'string' ? capTerminalName(name.trim()) || undefined : undefined
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
const NAME_KEY_PREFIX = 'mc-terminal-name:'
/** Dropped writes stay local until a successful rename, a foreign label event,
 *  or removal. Keep the fallback separate so layout writes cannot persist them. */
const volatileNames = new Map<string, { name?: string; fallback?: string }>()

function withLiveName(tab: TermTab): TermTab {
  const local = volatileNames.get(tab.id)
  return local ? (tab.name === local.name ? tab : { ...tab, name: local.name }) : withStoredName(tab)
}

/** Independent coordinates keep a rename from writing a stale tab list, and
 *  simultaneous renames of different tabs from replacing each other's labels.
 *  An empty string is an explicit reset, overriding names in layout backups. */
function withStoredName(tab: TermTab): TermTab {
  const stored = safeGetItem(NAME_KEY_PREFIX + tab.id)
  const name = normalizeTabName(stored === null ? tab.name : stored)
  return name === tab.name ? tab : { ...tab, name }
}

function withLiveNames(tabs: TermTab[]): TermTab[] {
  const merged = tabs.map(withLiveName)
  return merged.every((tab, i) => tab === tabs[i]) ? tabs : merged
}

/** Read membership afresh before label cleanup; malformed/unreadable storage
 *  cannot authorize deleting labels. No module-local list drives cleanup. */
function persistedIds(): Set<string> | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === null) return new Set()
    const p: unknown = JSON.parse(raw)
    if (!p || typeof p !== 'object' || Array.isArray(p)) return null
    const layout = p as { tabs?: unknown; splits?: unknown }
    const tabs = Array.isArray(layout.tabs) ? layout.tabs
      : Array.isArray(layout.splits) ? layout.splits
        : layout.tabs === undefined && layout.splits === undefined ? [] : null
    if (!tabs || tabs.some(t => !t || typeof t.id !== 'string')) return null
    return new Set(tabs.map(t => t.id as string))
  } catch { return null }
}

function cleanRemovedNames(ids: Iterable<string>): void {
  for (const id of ids) {
    const live = persistedIds()
    if (live && !live.has(id)) safeRemoveItem(NAME_KEY_PREFIX + id)
  }
}

/** Reclaim residue from an interrupted remove/rename, only for absent ids. */
function cleanOrphanNames(): void {
  try {
    const ids: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key?.startsWith(NAME_KEY_PREFIX)) ids.push(key.slice(NAME_KEY_PREFIX.length))
    }
    cleanRemovedNames(ids)
  } catch { /* unavailable storage */ }
}

/** Project labels into the existing uiPrefs backup without writing the layout.
 *  The backup remains self-contained when restored on a fresh origin. */
export function bottomTerminalPrefsSnapshot(raw: string): string {
  try {
    const p = JSON.parse(raw)
    if (!p || typeof p !== 'object' || Array.isArray(p)) return raw
    const field = Array.isArray(p.tabs) ? 'tabs' : Array.isArray(p.splits) ? 'splits' : null
    if (!field) return raw
    const tabs = p[field].map((tab: TermTab) => tab && typeof tab.id === 'string' ? withStoredName(tab) : tab)
    if (JSON.stringify(tabs) === JSON.stringify(p[field])) return raw
    return JSON.stringify({ ...p, [field]: tabs })
  } catch { return raw }
}
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

// A terminal tab id doubles as the PTY session id the backend addresses, so it
// is a security token and must not come from Math.random(); same rule as the
// chat-scoped terminal tabs in usePanelTabs.
const mintId = () => secureRandomId()
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
    const tabs = (rawTabs.filter(t => t && typeof (t as TermTab).id === 'string') as TermTab[])
      .slice(0, MAX_TERMINALS)
      .map(withLiveName)
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

cleanOrphanNames()
let state: BottomTerminalState = loadPersisted()
const listeners = new Set<() => void>()

function emit() { for (const cb of listeners) cb() }

/* ── Hydrate-time reconciliation ──
 * `loadPersisted` restores the tab LIST, not the shells: a tab is only a session
 * id, and the backend is the one that knows whether a PTY still answers to it.
 * A tab leaked by a dispatch that never reached its deadline (#10822), or one
 * whose shell the orphan reaper has since killed, therefore comes back on every
 * reload — occupying the tab cap, and re-spawning a fresh shell the moment its
 * view reconnects (the WS route mints a PTY for an unknown id).
 *
 * So the restored set is UNVERIFIED until `GET /api/terminal/sessions` has
 * ruled on it. While that is pending the hosts render no terminal views
 * (`useTerminalHydratePending`): a view that connected first would spawn the
 * very shell the probe is asking about, and the answer would then read "alive"
 * for a tab nobody wanted back. Only tabs restored at module init are
 * candidates — one minted after boot has a shell of its own and is never the
 * probe's business.
 *
 * The ruling takes two looks, not one, for the same reason the dispatch-deadline
 * probe does (ChatPage's run-in-terminal handler): the route skips the null
 * placeholder a session holds from `ws.prepare()` until its shell is spawned, so
 * a session another window is opening RIGHT NOW reads exactly like one that never
 * existed — and this store is shared across windows, so dropping it here would
 * remove the tab from under that shell as it comes up. `reconcileRestoredTabs`
 * therefore only names suspects; `confirmRestoredTabs`, fed an uncached second
 * answer after an opening grace, drops the ones still missing. Absent twice,
 * that far apart, is gone. */
let restoredIds: ReadonlySet<string> = new Set(state.tabs.map(t => t.id))
/** Restored tabs the first answer omitted or reported dead, awaiting the
 *  confirm probe. Empty outside the confirming phase. */
let hydrateSuspects: ReadonlySet<string> = new Set()
type HydratePhase = 'pending' | 'confirming' | 'settled'
let hydratePhase: HydratePhase = restoredIds.size > 0 ? 'pending' : 'settled'

/** Session ids the backend reports as live, or null when the payload does not
 *  rule on liveness: a transport failure, a shape this client does not
 *  recognize, or the feature-disabled answer (which returns an empty list
 *  without consulting the registry, so its absence means nothing). */
function liveSessionIds(payload: unknown): Set<string> | null {
  if (!payload || typeof payload !== 'object') return null
  const p = payload as { enabled?: unknown; sessions?: unknown }
  if (p.enabled === false || !Array.isArray(p.sessions)) return null
  const live = new Set<string>()
  for (const entry of p.sessions) {
    if (!entry || typeof entry !== 'object') return null
    const { session_id, alive } = entry as { session_id?: unknown; alive?: unknown }
    // One malformed entry voids the whole answer: dropping a tab is
    // irreversible, so it only happens on a payload read in full.
    if (typeof session_id !== 'string' || typeof alive !== 'boolean') return null
    if (alive) live.add(session_id)
  }
  return live
}

/** Leave the hydrate protocol with every remaining tab verified. */
function settleHydrate(): void {
  hydratePhase = 'settled'
  hydrateSuspects = new Set()
  emit()
}

/** Drop `ids` from the store, refocusing and hiding the panel as `removeTab`
 *  would. Persisting the trimmed list is what keeps the other window — and
 *  the next reload — from restoring the same tabs again. */
function dropTabs(ids: ReadonlySet<string>): void {
  const tabs = state.tabs.filter(t => !ids.has(t.id))
  const activeId = tabs.some(t => t.id === state.activeId) ? state.activeId : (tabs[0]?.id ?? null)
  set({ ...state, tabs, activeId, open: tabs.length > 0 ? state.open : false })
}

/** First look: weigh the restored tab set against the backend's session list
 *  (the JSON body of `GET /api/terminal/sessions`, or null when the probe
 *  failed). Restored tabs whose session is absent or `alive: false` become
 *  SUSPECTS and are returned; nothing is dropped yet, and the hosts stay gated
 *  until `confirmRestoredTabs` rules on them. With no suspects — or on a payload
 *  that does not rule (see `liveSessionIds`) — every tab is kept and the store
 *  settles at once: removing a possibly-live shell and its scrollback cannot be
 *  undone, while a kept dead tab is user-closable and its PTY entry is the
 *  reaper's to clear. Runs once per document: later calls return []. */
export function reconcileRestoredTabs(payload: unknown): string[] {
  if (hydratePhase !== 'pending') return []
  const live = liveSessionIds(payload)
  const suspects = live === null
    ? []
    : state.tabs.filter(t => restoredIds.has(t.id) && !live.has(t.id)).map(t => t.id)
  if (suspects.length === 0) { settleHydrate(); return [] }
  hydratePhase = 'confirming'
  hydrateSuspects = new Set(suspects)
  return suspects
}

/** Second look, from an UNCACHED probe taken after the opening grace: drop the
 *  suspects this answer still omits or reports dead, keep the ones it now lists
 *  live (a shell that was opening in another window), and settle. A payload
 *  that does not rule keeps every suspect. Returns the dropped ids. */
export function confirmRestoredTabs(payload: unknown): string[] {
  if (hydratePhase !== 'confirming') return []
  const live = liveSessionIds(payload)
  const dropped = live === null
    ? []
    : state.tabs.filter(t => hydrateSuspects.has(t.id) && !live.has(t.id)).map(t => t.id)
  // Drop while still gated, then settle: the hosts first see the kept set.
  if (dropped.length > 0) dropTabs(new Set(dropped))
  settleHydrate()
  return dropped
}

/* Cross-window sync: the terminal-popout window and the main dashboard share
 * this persisted store (one tab list, whichever window currently hosts the
 * panel). `storage` fires only in OTHER windows — never the writer — so
 * re-loading here can't loop; state is adopted without re-persisting. */
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.storageArea && e.storageArea !== localStorage) return
    if (e.key === null || e.key === STORAGE_KEY) {
      if (e.key === null) volatileNames.clear()
      state = loadPersisted()
      for (const id of volatileNames.keys()) {
        if (!state.tabs.some(tab => tab.id === id)) volatileNames.delete(id)
      }
    } else if (e.key?.startsWith(NAME_KEY_PREFIX)) {
      // Read current values, not an event payload queued before a later save.
      // A label event cannot add/drop tabs or change the local layout.
      const id = e.key.slice(NAME_KEY_PREFIX.length)
      volatileNames.delete(id)
      const stored = loadPersisted()
      state = { ...state, tabs: state.tabs.map(tab => tab.id === id
        ? { ...tab, name: stored.tabs.find(t => t.id === id)?.name }
        : tab) }
    } else return
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
  const removed = state.tabs.filter(tab => !next.tabs.some(t => t.id === tab.id)).map(tab => tab.id)
  for (const id of removed) volatileNames.delete(id)
  state = { ...next, tabs: withLiveNames(next.tabs) }
  emit()
  const tabs = state.tabs.map(tab => {
    const local = volatileNames.get(tab.id)
    return local ? withStoredName({ ...tab, name: local.fallback }) : tab
  })
  try { safeSetItem(STORAGE_KEY, JSON.stringify({ ...state, tabs })) } catch { /* quota / locked storage */ }
  cleanRemovedNames(removed)
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

/** Whether a tab with this session id is currently in the store. Lets the
 *  run-in-terminal dispatch check, at its deadline, that the tab it minted is
 *  still its own to roll back — a tab the user already closed is gone from
 *  here, and rolling back anyway would double-delete the PTY. */
export function hasTab(id: string): boolean {
  return state.tabs.some(t => t.id === id)
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

/** Change only the label, never the PTY identity or working directory.
 *  An empty name returns the tab to the live shell title. */
export function renameTab(id: string, value: string): void {
  const name = normalizeTabName(value)
  const tab = state.tabs.find(t => t.id === id)
  if (!tab) return
  const live = persistedIds()
  const saved = live?.has(id) && safeSetItem(NAME_KEY_PREFIX + id, name ?? '')
  if (saved) volatileNames.delete(id)
  else {
    const prior = volatileNames.get(id)
    volatileNames.set(id, { name, fallback: prior ? prior.fallback : tab.name })
  }
  if (live?.has(id)) {
    // A close may interleave between membership read and label write. It wins;
    // the label cannot resurrect its tab or remain as a new orphan coordinate.
    cleanRemovedNames([id])
  }
  state = { ...state, tabs: state.tabs.map(t => t.id === id ? { ...t, name } : t) }
  emit()
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

/** Imperative read of the same flag, for non-React callers that must know which
 *  way a `toggleBottomTerminal` is about to go — see `terminalChordFocus.ts`,
 *  which has to decide about focus BEFORE the panel unmounts. */
export function isBottomTerminalOpen(): boolean { return state.open }

/** Selector for just the `position` field. */
function getPositionSnapshot(): TerminalPosition { return state.position }
export function useTerminalPosition(): TerminalPosition {
  return useSyncExternalStore(subscribe, getPositionSnapshot, getPositionSnapshot)
}

/** Whether the restored tab set still awaits its ruling (either look). Hosts
 *  mount no terminal view while true — see the reconciliation note above. */
function getHydratePendingSnapshot(): boolean { return hydratePhase !== 'settled' }
export function isTerminalHydratePending(): boolean { return hydratePhase !== 'settled' }
export function useTerminalHydratePending(): boolean {
  return useSyncExternalStore(subscribe, getHydratePendingSnapshot, getHydratePendingSnapshot)
}

/** Test-only: reset the module store and its persisted copy. */
export function __resetBottomTerminal(): void {
  volatileNames.clear()
  state = { open: false, height: DEFAULT_HEIGHT, width: DEFAULT_WIDTH, position: 'bottom', tabs: [], activeId: null }
  restoredIds = new Set()
  hydrateSuspects = new Set()
  hydratePhase = 'settled'
  emit()
  setTerminalCloseFailed(false)
  if (typeof localStorage !== 'undefined') {
    try { localStorage.removeItem(STORAGE_KEY) } catch { /* ignore */ }
    cleanOrphanNames()
  }
}

/* ── Close-failure notice ──
 * A rejected PTY DELETE for a tab that is already gone locally. A boolean flag,
 * kept OUTSIDE the persisted layout state above, mirrored to localStorage under
 * its own key purely as a cross-WINDOW transport: the popout frame returns
 * itself to the main window the moment its last tab closes, and the main
 * window's always-mounted panel root is then the surface the notice lands on.
 * It is deliberately NOT read at module init — a report the server-side reaper
 * backstops must not greet the next launch — and it is a flag rather than a
 * rendered string so the reader window renders it in its own locale. The strip
 * is never the host: closing the LAST tab unmounts it before a delayed
 * rejection can render. */
const CLOSE_ERROR_KEY = 'mc-terminal-close-error'
let closeFailed = false
const closeErrorListeners = new Set<() => void>()
function emitCloseError() { for (const cb of closeErrorListeners) cb() }
function subscribeCloseError(cb: () => void) {
  closeErrorListeners.add(cb)
  return () => { closeErrorListeners.delete(cb) }
}
function getCloseFailed(): boolean { return closeFailed }
export function setTerminalCloseFailed(failed: boolean): void {
  if (failed === closeFailed) return
  closeFailed = failed
  emitCloseError()
  if (typeof localStorage === 'undefined') return
  try {
    // A UNIQUE value per failure, not a constant: `storage` only fires when the
    // stored value changes, so a constant retained from a session that was never
    // dismissed (the key is deliberately not read at launch) would swallow the
    // next failure's event. Readers test presence, never the value.
    if (failed) safeSetItem(CLOSE_ERROR_KEY, String(Date.now()))
    else localStorage.removeItem(CLOSE_ERROR_KEY)
  } catch { /* quota / locked storage — the in-window notice still rendered */ }
}
export function useTerminalCloseFailed(): boolean {
  return useSyncExternalStore(subscribeCloseError, getCloseFailed, getCloseFailed)
}
if (typeof window !== 'undefined') {
  // `storage` fires only in OTHER windows, so adopting here cannot loop.
  window.addEventListener('storage', (e) => {
    if (e.key !== CLOSE_ERROR_KEY) return
    closeFailed = e.newValue != null
    emitCloseError()
  })
}
