import { normalizeRunSessionKey } from '../apps/workflows/runModel'
import { withTerminalStateLock, readTerminalStateSnapshot, __resetTerminalStateForTests, type TerminalStateSnapshot } from '../utils/terminalStateLock'
import { useEffect, useSyncExternalStore } from 'react'
import { safeSetItem } from '../utils/safeStorage'
import { secureRandomId } from '../utils/secureId'

/** Session-owned terminal tabs and visibility; dimensions/docking stay shared.
 * Explicit scope arguments bind delayed callbacks to their original chat.
 * Unmounting a view never disposes its cached xterm or persistent connection. */
export type TerminalSessionScope = string | null

export interface TermTab {
  /** PTY session id — one live shell per tab. */
  id: string
  /** Working directory the shell spawned in (undefined = server default). */
  cwd?: string
}

/** Where the terminal panel is docked — like VS Code's Panel position. */
export type TerminalPosition = 'bottom' | 'right'

export interface BottomTerminalState {
  open: boolean
  /** Docked terminal count across all chat sessions, for the shared cap. */
  totalTabs: number
  /** Changes only when a deleted conversation is retired or deliberately reopened. */
  epoch: number
  retired: boolean
  preparing: boolean
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
/** Max docked terminal tabs across ALL chat sessions (each is a live PTY). */
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

type SessionState = Pick<BottomTerminalState, 'open' | 'tabs' | 'activeId'>
type LayoutState = Pick<BottomTerminalState, 'height' | 'width' | 'position'>
const SESSION_PREFIX = 'mc-bottom-terminal-session:'
const LAYOUT_KEY = 'mc-bottom-terminal-layout'
const MIGRATION_KEY = 'mc-bottom-terminal-migration'
const UPDATE_KEY = 'mc-bottom-terminal-updated'
const LIFECYCLE_PREFIX = 'mc-bottom-terminal-lifecycle:'
let canonicalValues = new Map<string, string>()
let transactionValues: Map<string, string> | null = null
let publishedRevision = -1
let preparedSnapshot: TerminalSessionLease[] = []
let generation = 0
const pendingRetirements = new Map<TerminalSessionScope, number>()
const EMPTY: SessionState = { open: false, tabs: [], activeId: null }
const DEFAULT_LAYOUT: LayoutState = { height: DEFAULT_HEIGHT, width: DEFAULT_WIDTH, position: 'bottom' }
export function terminalSessionStorageKey(scope: TerminalSessionScope = null): string {
  return SESSION_PREFIX + (scope === null ? 'unassigned' : `session:${encodeURIComponent(scope)}`)
}
export interface TerminalSessionLease { scope: TerminalSessionScope; epoch: number; token?: string }
export function normalizeTerminalScope(scope: TerminalSessionScope): TerminalSessionScope {
  if (scope === null) return null
  let next = normalizeRunSessionKey(scope)
  while (next !== scope) { scope = next; next = normalizeRunSessionKey(scope) }
  return next
}
function lifecycleKey(scope: TerminalSessionScope): string {
  return LIFECYCLE_PREFIX + JSON.stringify(normalizeTerminalScope(scope))
}
function lifecycle(scope: TerminalSessionScope) {
  const value = read(lifecycleKey(scope))
  return { epoch: typeof value.epoch === 'number' ? value.epoch : 0, retired: value.retired === true, preparing: typeof value.deletingToken === 'string' }
}
export function captureTerminalSessionLease(scope: TerminalSessionScope): TerminalSessionLease {
  return { scope: normalizeTerminalScope(scope), epoch: lifecycle(scope).epoch }
}
export function terminalStateLoaded(): boolean { return publishedRevision >= 0 }
function retirementPending(scope: TerminalSessionScope): boolean {
  return pendingRetirements.get(normalizeTerminalScope(scope)) === lifecycle(scope).epoch
}
/** Server confirmation hides this generation immediately while its local cleanup retries. */
export function markTerminalRetirementPending(lease: TerminalSessionLease): void {
  pendingRetirements.set(lease.scope, lease.epoch)
  emit()
}
/** Read committed scope identity without changing membership or lifecycle. */
export async function readTerminalSessionLease(scope: string): Promise<TerminalSessionLease> {
  const started = generation
  const snapshot = await readTerminalStateSnapshot()
  if (started !== generation) throw new Error('terminal-state-reset')
  publish(snapshot)
  return captureTerminalSessionLease(scope)
}
/** Reserve the existing generation without hiding or disposing its terminals.
 * This authoritative fence survives an ordinary post-HTTP retirement failure
 * in another window. It is cancelled if the server does not confirm deletion. */
export async function prepareTerminalRetirement(scope: string): Promise<TerminalSessionLease> {
  const started = generation
  const token = secureRandomId()
  const leaseScope = normalizeTerminalScope(scope)
  const committed = await withTerminalStateLock((values, revision) => {
    if (started !== generation) throw new Error('terminal-state-reset')
    if (revision === 0) bootstrap(values)
    const key = lifecycleKey(leaseScope)
    const current = parse(values.get(key) ?? null)
    if (current.deletingToken) throw new Error('terminal-retirement-in-progress')
    const epoch = Number(current.epoch ?? 0)
    values.set(key, JSON.stringify({ ...current, epoch, deletingToken: token }))
    return { scope: leaseScope, epoch, token }
  })
  if (started !== generation) throw new Error('terminal-state-reset')
  publish(committed.snapshot)
  safeSetItem(UPDATE_KEY, String(committed.snapshot.revision))
  return committed.result
}
export async function cancelTerminalRetirement(lease: TerminalSessionLease): Promise<void> {
  const started = generation
  const committed = await withTerminalStateLock(values => {
    if (started !== generation) return
    const key = lifecycleKey(lease.scope)
    const current = parse(values.get(key) ?? null)
    if (current.deletingToken !== lease.token || Number(current.epoch ?? 0) !== lease.epoch) return
    delete current.deletingToken
    values.set(key, JSON.stringify(current))
  })
  if (started !== generation) return
  publish(committed.snapshot)
  safeSetItem(UPDATE_KEY, String(committed.snapshot.revision))
}
const retirementListeners = new Set<(ids: string[]) => void>()
export function subscribeTerminalRetirement(listener: (ids: string[]) => void): () => void {
  retirementListeners.add(listener)
  return () => { retirementListeners.delete(listener) }
}

function parse(raw: string | null): Record<string, unknown> {
  try { const value = JSON.parse(raw || '{}'); return value && typeof value === 'object' ? value : {} } catch { return {} }
}
function readLocal(key: string): Record<string, unknown> {
  try { return parse(localStorage.getItem(key)) } catch { return {} }
}
function read(key: string): Record<string, unknown> {
  return parse((transactionValues ?? canonicalValues).get(key) ?? null)
}
function sessionFrom(value: Record<string, unknown>): SessionState {
  const raw = Array.isArray(value.tabs) ? value.tabs : Array.isArray(value.splits) ? value.splits : []
  // Never truncate recovered tabs: preserving existing PTYs takes precedence
  // over the creation cap, which still prevents any additional allocation.
  const seen = new Set<string>()
  const tabs = raw.filter((tab): tab is TermTab => {
    if (!tab || typeof tab.id !== 'string' || seen.has(tab.id)) return false
    seen.add(tab.id); return true
  })
  return { tabs, open: value.open === true && tabs.length > 0,
    activeId: tabs.some(tab => tab.id === value.activeId) ? value.activeId as string : tabs[0]?.id ?? null }
}
function layoutFrom(value: Record<string, unknown>): LayoutState {
  return {
    height: typeof value.height === 'number' && Number.isFinite(value.height) ? clampHeight(value.height) : DEFAULT_HEIGHT,
    width: typeof value.width === 'number' && Number.isFinite(value.width) ? clampWidth(value.width) : DEFAULT_WIDTH,
    position: value.position === 'right' ? 'right' : 'bottom',
  }
}
let layout = layoutFrom(Object.keys(readLocal(LAYOUT_KEY)).length ? readLocal(LAYOUT_KEY) : readLocal(STORAGE_KEY))
let legacy: SessionState | null = null
let sessions = new Map<string, SessionState>()
const listeners = new Set<() => void>()
let revision = 0
const snapshots = new Map<string, { revision: number; state: BottomTerminalState }>()
function invalidate() { revision++; snapshots.clear() }
function emit() { invalidate(); for (const cb of listeners) cb() }
function loadSessions() {
  const migration = read(MIGRATION_KEY)
  legacy = migration.completed === true ? null : sessionFrom(read(STORAGE_KEY))
  sessions = new Map()
  for (const [key, value] of transactionValues ?? canonicalValues) {
    if (key.startsWith(SESSION_PREFIX)) sessions.set(key, sessionFrom(parse(value)))
  }
  if (legacy && sessions.has(terminalSessionStorageKey(null))) legacy = EMPTY
}
function bootstrap(values: Map<string, string>) {
  const value = localStorage.getItem(STORAGE_KEY)
  if (value !== null) values.set(STORAGE_KEY, value)
}

function publish(snapshot: TerminalStateSnapshot) {
  if (snapshot.revision <= publishedRevision) return
  publishedRevision = snapshot.revision
  const removed: string[] = []
  for (const [key, value] of canonicalValues) {
    if (!key.startsWith(SESSION_PREFIX)) continue
    const suffix = key.slice(SESSION_PREFIX.length)
    if (!suffix.startsWith('session:')) continue
    const scope = decodeURIComponent(suffix.slice('session:'.length))
    const nextLife = parse(snapshot.values.get(lifecycleKey(scope)) ?? null)
    if (Number(nextLife.epoch ?? 0) > lifecycle(scope).epoch) removed.push(...sessionFrom(parse(value)).tabs.map(tab => tab.id))
  }
  canonicalValues = snapshot.values
  preparedSnapshot = []
  for (const [key, raw] of canonicalValues) {
    if (!key.startsWith(LIFECYCLE_PREFIX)) continue
    const value = parse(raw)
    if (typeof value.deletingToken !== 'string') continue
    const scope = JSON.parse(key.slice(LIFECYCLE_PREFIX.length)) as TerminalSessionScope
    preparedSnapshot.push({ scope, epoch: Number(value.epoch ?? 0), token: value.deletingToken })
  }
  loadSessions()
  if (removed.length) for (const listener of retirementListeners) {
    try { listener(removed) } catch (error) {
      // A view failure must not turn a committed retirement into an IDB failure.
      // eslint-disable-next-line no-console
      console.error('Failed to dispose retired terminal view', error)
    }
  }
  emit()
}
async function refreshCanonical() {
  const started = generation
  try {
    const snapshot = await readTerminalStateSnapshot()
    if (started === generation && snapshot.revision > 0) publish(snapshot)
  } catch { /* The session initializer renders any unavailable-store error. */ }
}
function getSession(scope: TerminalSessionScope = null): SessionState {
  if (retirementPending(scope)) return EMPTY
  if (scope === null && legacy) return mergeSessions(sessions.get(terminalSessionStorageKey(null)) ?? EMPTY, legacy)
  return sessions.get(terminalSessionStorageKey(scope)) ?? EMPTY
}
function mergeSessions(existing: SessionState, incoming: SessionState): SessionState {
  const ids = new Set(existing.tabs.map(tab => tab.id))
  return { tabs: [...existing.tabs, ...incoming.tabs.filter(tab => !ids.has(tab.id))],
    open: existing.open || incoming.open, activeId: existing.activeId ?? incoming.activeId }
}
function persist(key: string, value: unknown) {
  if (!transactionValues) throw new Error('terminal-state-transaction')
  transactionValues.set(key, JSON.stringify(value))
}
/** Legacy membership and its completed owner commit in the same transaction. */
function migrateLegacy(scope: TerminalSessionScope) {
  const migration = read(MIGRATION_KEY)
  if (migration.completed === true) return
  const owner = scope
  if (owner === null) return
  const key = terminalSessionStorageKey(owner)
  const existing = sessions.get(key) ?? EMPTY
  const pending = sessions.get(terminalSessionStorageKey(null)) ?? sessionFrom(read(STORAGE_KEY))
  const adopted = mergeSessions(existing, pending)
  persist(key, adopted)
  // Pending no-selection work travels with the first real owner, exactly once.
  persist(terminalSessionStorageKey(null), EMPTY)
  persist(MIGRATION_KEY, { owner, completed: true })
  sessions.set(key, adopted)
  sessions.set(terminalSessionStorageKey(null), EMPTY)
  legacy = null
}
const stateFailures = new Set<TerminalSessionScope>()
const capacityFailures = new Set<TerminalSessionScope>()
export function useTerminalCapacityExceeded(scope: TerminalSessionScope = null): boolean {
  return useSyncExternalStore(subscribe, () => capacityFailures.has(scope), () => capacityFailures.has(scope))
}
export function clearTerminalCapacityFailure(scope: TerminalSessionScope = null) { capacityFailures.delete(scope); emit() }
function reportCapacity(scope: TerminalSessionScope, success: boolean) {
  if (stateFailures.has(scope) || lifecycle(scope).retired || lifecycle(scope).preparing || retirementPending(scope)) return
  const before = capacityFailures.has(scope)
  if (success) capacityFailures.delete(scope)
  else capacityFailures.add(scope)
  if (before !== capacityFailures.has(scope)) emit()
}
export function useTerminalStateFailed(scope: TerminalSessionScope = null): boolean {
  return useSyncExternalStore(subscribe, () => stateFailures.has(scope), () => stateFailures.has(scope))
}
export function clearTerminalStateFailure(scope: TerminalSessionScope = null) { stateFailures.delete(scope); emit() }
/** Confirm a closed popout's committed membership before reconnecting its PTYs.
 * This is a readonly barrier: it never bootstraps, migrates, opens, or mints. */
export async function refreshTerminalState(scope: TerminalSessionScope): Promise<boolean> {
  const started = generation
  try {
    const snapshot = await readTerminalStateSnapshot()
    if (started !== generation) return false
    // Revision zero means no committed authority exists. Reusing the old
    // in-memory snapshot here could recreate a terminal that was just deleted.
    if (snapshot.revision === 0) { stateFailures.add(scope); emit(); return false }
    publish(snapshot)
    if (stateFailures.delete(scope)) emit()
    return true
  } catch {
    if (started === generation) { stateFailures.add(scope); emit() }
    return false
  }
}

function inSession<T>(scope: TerminalSessionScope, operation: () => T, fallback: T, lease = captureTerminalSessionLease(scope), allowPrepared = false): Promise<T> {
  const started = generation
  return withTerminalStateLock((values, revision) => {
    if (revision === 0) bootstrap(values)
    const beforeSessions = sessions
    const beforeLegacy = legacy
    transactionValues = values
    try {
      // Compute against an isolated authoritative snapshot. React keeps the
      // previous committed snapshot until IndexedDB confirms the transaction.
      loadSessions()
      const current = lifecycle(scope)
      if (current.retired || (current.preparing && !allowPrepared) || retirementPending(scope) || current.epoch !== lease.epoch || normalizeTerminalScope(scope) !== lease.scope) return fallback
      if (!current.preparing) migrateLegacy(scope)
      return operation()
    } finally {
      sessions = beforeSessions
      legacy = beforeLegacy
      transactionValues = null
    }
  }).then(({ result, snapshot }) => {
    if (started !== generation) return fallback
    const clearedFailure = stateFailures.delete(scope)
    const stale = snapshot.revision <= publishedRevision
    publish(snapshot)
    if (clearedFailure && stale) emit()
    safeSetItem(UPDATE_KEY, String(snapshot.revision))
    return result
  }).catch(() => {
    if (started === generation) { stateFailures.add(scope); emit() }
    return fallback
  })
}
/** Only call after a successful server activation, using its pre-request lease.
 * A cold popup or ordinary initializer cannot revive a permanently deleted scope. */
export async function activateTerminalSession(scope: string, lease: TerminalSessionLease): Promise<void> {
  const started = generation
  try {
    const committed = await withTerminalStateLock(values => {
      if (started !== generation) return
      const key = lifecycleKey(scope)
      const current = parse(values.get(key) ?? null)
      if (!current.deletingToken && normalizeTerminalScope(scope) === lease.scope && current.retired === true && Number(current.epoch ?? 0) === lease.epoch) {
        values.set(key, JSON.stringify({ epoch: lease.epoch + 1, retired: false }))
      }
    })
    if (started !== generation) return
    publish(committed.snapshot)
    safeSetItem(UPDATE_KEY, String(committed.snapshot.revision))
  } catch {
    if (started === generation) { stateFailures.add(scope); emit() }
  }
}
/** A confirmed permanent deletion frees every alias in one transaction. */
export async function retireTerminalSession(lease: TerminalSessionLease): Promise<string[]> {
  const started = generation
  const committed = await withTerminalStateLock(values => {
    if (started !== generation) throw new Error('terminal-state-reset')
    const key = lifecycleKey(lease.scope)
    const current = parse(values.get(key) ?? null)
    if (Number(current.epoch ?? 0) !== lease.epoch || current.retired === true) return []
    if (current.deletingToken && current.deletingToken !== lease.token) throw new Error('terminal-retirement-in-progress')
    const ids = new Set<string>()
    for (const [bucketKey, raw] of values) {
      if (!bucketKey.startsWith(SESSION_PREFIX + 'session:')) continue
      const scope = decodeURIComponent(bucketKey.slice((SESSION_PREFIX + 'session:').length))
      if (normalizeTerminalScope(scope) !== lease.scope) continue
      sessionFrom(parse(raw)).tabs.forEach(tab => ids.add(tab.id))
      values.set(bucketKey, JSON.stringify(EMPTY))
    }
    values.set(key, JSON.stringify({ epoch: lease.epoch + 1, retired: true }))
    return [...ids]
  })
  if (started !== generation) throw new Error('terminal-state-reset')
  if (pendingRetirements.get(lease.scope) === lease.epoch) pendingRetirements.delete(lease.scope)
  publish(committed.snapshot)
  safeSetItem(UPDATE_KEY, String(committed.snapshot.revision))
  return committed.result
}

export function initializeTerminalSession(scope: TerminalSessionScope): Promise<void> {
  return inSession(scope, () => {}, undefined, undefined, true)
}
function totalTabs(): number {
  const ids = new Set(legacy?.tabs.map(tab => tab.id))
  sessions.forEach(value => value.tabs.forEach(tab => ids.add(tab.id)))
  return ids.size
}
function setSession(scope: TerminalSessionScope, next: SessionState) {
  persist(terminalSessionStorageKey(scope), next)
  sessions.set(terminalSessionStorageKey(scope), next)
  // Until migration, null is the editable ownerless view. Its bucket is the
  // current version, so do not merge the untouched legacy file back into it.
  if (scope === null && legacy) legacy = EMPTY
}
function setLayout(next: LayoutState) { layout = next; safeSetItem(LAYOUT_KEY, JSON.stringify(next)); emit() }

/** Open this session's panel, minting a first tab only under the shared cap. */
function openBottomTerminalLocked(cwd?: string, scope: TerminalSessionScope = null): boolean {
  const state = getSession(scope)
  if (!state.tabs.length && (lifecycle(scope).preparing || totalTabs() >= MAX_TERMINALS)) return false
  const tabs = state.tabs.length ? state.tabs : [{ id: mintId(), cwd }]
  setSession(scope, { ...state, open: true, tabs, activeId: state.activeId ?? tabs[0].id })
  return true
}
function closeBottomTerminalLocked(scope: TerminalSessionScope = null): void {
  const state = getSession(scope)
  if (state.open) setSession(scope, { ...state, open: false })
}
function toggleBottomTerminalLocked(cwd?: string, scope: TerminalSessionScope = null): boolean {
  if (getSession(scope).open) { closeBottomTerminalLocked(scope); return true }
  return openBottomTerminalLocked(cwd, scope)
}
function addTabLocked(cwd?: string, scope: TerminalSessionScope = null): string | null {
  const state = getSession(scope)
  if (totalTabs() >= MAX_TERMINALS) {
    const last = state.tabs[state.tabs.length - 1]
    if (last) setSession(scope, { ...state, open: true, activeId: last.id })
    return null
  }
  const id = mintId()
  setSession(scope, { ...state, open: true, tabs: [...state.tabs, { id, cwd }], activeId: id })
  return id
}
function adoptTabLocked(id: string, cwd?: string, scope: TerminalSessionScope = null): boolean {
  const state = getSession(scope)
  if (state.tabs.some(tab => tab.id === id)) { setSession(scope, { ...state, open: true, activeId: id }); return true }
  // A PTY belongs to exactly one session bucket. Never alias a socket across chats.
  if (totalTabs() >= MAX_TERMINALS || Array.from(sessions.values()).some(s => s.tabs.some(t => t.id === id))) return false
  setSession(scope, { ...state, open: true, tabs: [...state.tabs, { id, cwd }], activeId: id })
  return true
}
function removeTabLocked(id: string, scope: TerminalSessionScope = null): boolean {
  const state = getSession(scope)
  const idx = state.tabs.findIndex(tab => tab.id === id)
  if (idx < 0) return false
  const tabs = state.tabs.filter(tab => tab.id !== id)
  const activeId = state.activeId !== id ? state.activeId : (tabs[idx - 1] ?? tabs[idx] ?? tabs[tabs.length - 1])?.id ?? null
  setSession(scope, { ...state, tabs, activeId, open: tabs.length > 0 && state.open })
  return true
}
function setActiveTabLocked(id: string, scope: TerminalSessionScope = null): void {
  const state = getSession(scope)
  if (state.activeId !== id && state.tabs.some(tab => tab.id === id)) setSession(scope, { ...state, activeId: id })
}
function setTabsOrderLocked(next: TermTab[], scope: TerminalSessionScope = null): void {
  const state = getSession(scope)
  // A stale drag cannot add/remove tabs created or closed while it was active.
  const byId = new Map(state.tabs.map(tab => [tab.id, tab]))
  const tabs = next.flatMap(tab => { const current = byId.get(tab.id); byId.delete(tab.id); return current ? [current] : [] })
  tabs.push(...byId.values())
  setSession(scope, { ...state, tabs })
}
export async function openBottomTerminal(cwd?: string, scope: TerminalSessionScope = null): Promise<boolean> {
  const started = generation
  const lease = captureTerminalSessionLease(scope)
  const success = await inSession(scope, () => openBottomTerminalLocked(cwd, scope), false, lease, true)
  if (started === generation && lifecycle(scope).epoch === lease.epoch) reportCapacity(scope, success)
  return success
}
export const closeBottomTerminal = (scope: TerminalSessionScope = null): Promise<void> => inSession(scope, () => closeBottomTerminalLocked(scope), undefined, undefined, true)
export async function toggleBottomTerminal(cwd?: string, scope: TerminalSessionScope = null): Promise<void> {
  const started = generation
  const lease = captureTerminalSessionLease(scope)
  const success = await inSession(scope, () => toggleBottomTerminalLocked(cwd, scope), false, lease, true)
  if (started === generation && lifecycle(scope).epoch === lease.epoch) reportCapacity(scope, success)
}
export const addTab = (cwd?: string, scope: TerminalSessionScope = null, lease?: TerminalSessionLease): Promise<string | null> => inSession(scope, () => addTabLocked(cwd, scope), null, lease)
export const adoptTab = (id: string, cwd?: string, scope: TerminalSessionScope = null, lease?: TerminalSessionLease): Promise<boolean> => inSession(scope, () => adoptTabLocked(id, cwd, scope), false, lease)
export const removeTab = (id: string, scope: TerminalSessionScope = null): Promise<boolean> => inSession(scope, () => removeTabLocked(id, scope), false, undefined, true)
export const setActiveTab = (id: string, scope: TerminalSessionScope = null): Promise<void> => inSession(scope, () => setActiveTabLocked(id, scope), undefined, undefined, true)
export const setTabsOrder = (tabs: TermTab[], scope: TerminalSessionScope = null): Promise<void> => inSession(scope, () => setTabsOrderLocked(tabs, scope), undefined, undefined, true)
export function setBottomTerminalHeight(px: number): void {
  const height = clampHeight(px); if (height !== layout.height) setLayout({ ...layout, height })
}
export function setBottomTerminalWidth(px: number): void {
  const width = clampWidth(px); if (width !== layout.width) setLayout({ ...layout, width })
}
export function setTerminalPosition(position: TerminalPosition): void {
  if (position !== layout.position) setLayout({ ...layout, position })
}
export function toggleTerminalPosition(): void { setTerminalPosition(layout.position === 'bottom' ? 'right' : 'bottom') }
function subscribe(cb: () => void) { listeners.add(cb); return () => { listeners.delete(cb) } }
export function getBottomTerminalSnapshot(scope: TerminalSessionScope = null): BottomTerminalState {
  const state = getSession(scope)
  const key = terminalSessionStorageKey(scope)
  const cached = snapshots.get(key)
  if (cached?.revision === revision) return cached.state
  const result = { ...layout, ...state, ...lifecycle(scope), retired: lifecycle(scope).retired || retirementPending(scope), totalTabs: totalTabs(), height: clampToViewport(layout.height, 'height'), width: clampToViewport(layout.width, 'width') }
  snapshots.set(key, { revision, state: result })
  return result
}
export function useBottomTerminal(scope: TerminalSessionScope = null): BottomTerminalState {
  useEffect(() => { void initializeTerminalSession(scope) }, [scope])
  return useSyncExternalStore(subscribe, () => getBottomTerminalSnapshot(scope), () => getBottomTerminalSnapshot(scope))
}
export function isBottomTerminalOpen(scope: TerminalSessionScope = null): boolean { return getSession(scope).open }
export function useBottomTerminalOpen(scope: TerminalSessionScope = null): boolean {
  useEffect(() => { void initializeTerminalSession(scope) }, [scope])
  return useSyncExternalStore(subscribe, () => isBottomTerminalOpen(scope), () => isBottomTerminalOpen(scope))
}
export function usePreparedTerminalRetirements(): TerminalSessionLease[] {
  return useSyncExternalStore(subscribe, () => preparedSnapshot, () => preparedSnapshot)
}
export function useTerminalRevivalNeeded(scope: TerminalSessionScope): boolean {
  const ready = () => lifecycle(scope).retired && !lifecycle(scope).preparing
  return useSyncExternalStore(subscribe, ready, ready)
}
export function useTerminalPosition(): TerminalPosition {
  return useSyncExternalStore(subscribe, () => layout.position, () => layout.position)
}
if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key === LAYOUT_KEY) { layout = layoutFrom(readLocal(LAYOUT_KEY)); emit() }
    else if (event.key === UPDATE_KEY) void refreshCanonical()
    else if (event.key === null) { layout = { ...DEFAULT_LAYOUT }; emit(); void refreshCanonical() }
  })
  window.addEventListener('resize', () => {
    const nextHeight = clampToViewport(layout.height, 'height')
    const nextWidth = clampToViewport(layout.width, 'width')
    if (Array.from(snapshots.values()).some(({ state }) => state.height !== nextHeight || state.width !== nextWidth)) emit()
  })
}
/** Test-only renderer reset. A fixture with a fresh IDBFactory already has
 * an empty database; clearing it again would unnecessarily depend on timers. */
export function __resetBottomTerminalRenderer(): void {
  generation++
  pendingRetirements.clear()
  preparedSnapshot = []
  canonicalValues = new Map(); publishedRevision = -1
  sessions.clear(); layout = { ...DEFAULT_LAYOUT }; legacy = null
  try {
    const keys: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key?.startsWith(SESSION_PREFIX)) keys.push(key)
    }
    keys.forEach(key => localStorage.removeItem(key))
    localStorage.removeItem(STORAGE_KEY)
    localStorage.removeItem(LAYOUT_KEY)
    localStorage.removeItem(MIGRATION_KEY)
    localStorage.removeItem(UPDATE_KEY)
  } catch { /* locked storage */ }
  stateFailures.clear(); capacityFailures.clear(); emit(); closeFailures.clear(); emitCloseError()
}

/** Test-only full reset for tests reusing their current database factory. */
export async function __resetBottomTerminal(): Promise<void> {
  generation++
  pendingRetirements.clear()
  preparedSnapshot = []
  await __resetTerminalStateForTests()
  __resetBottomTerminalRenderer()
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
const closeFailures = new Map<string, boolean>()
const closeErrorKey = (scope: TerminalSessionScope) => CLOSE_ERROR_KEY + (scope === null ? '' : `:${encodeURIComponent(scope)}`)
const closeErrorListeners = new Set<() => void>()
function emitCloseError() { for (const cb of closeErrorListeners) cb() }
function subscribeCloseError(cb: () => void) {
  closeErrorListeners.add(cb)
  return () => { closeErrorListeners.delete(cb) }
}
export function setTerminalCloseFailed(failed: boolean, scope: TerminalSessionScope = null): void {
  const key = closeErrorKey(scope)
  if (failed === (closeFailures.get(key) ?? false)) return
  closeFailures.set(key, failed)
  emitCloseError()
  if (typeof localStorage === 'undefined') return
  try {
    // A UNIQUE value per failure, not a constant: `storage` only fires when the
    // stored value changes, so a constant retained from a session that was never
    // dismissed (the key is deliberately not read at launch) would swallow the
    // next failure's event. Readers test presence, never the value.
    if (failed) safeSetItem(key, secureRandomId())
    else localStorage.removeItem(key)
  } catch { /* quota / locked storage — the in-window notice still rendered */ }
}
export function useTerminalCloseFailed(scope: TerminalSessionScope = null): boolean {
  const get = () => closeFailures.get(closeErrorKey(scope)) ?? false
  return useSyncExternalStore(subscribeCloseError, get, get)
}
if (typeof window !== 'undefined') {
  // `storage` fires only in OTHER windows, so adopting here cannot loop.
  window.addEventListener('storage', (e) => {
    if (!e.key || (e.key !== CLOSE_ERROR_KEY && !e.key.startsWith(CLOSE_ERROR_KEY + ':'))) return
    closeFailures.set(e.key, e.newValue != null)
    emitCloseError()
  })
}
