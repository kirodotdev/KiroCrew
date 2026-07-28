import { useCallback, useMemo, useSyncExternalStore } from 'react'
import type { Artifact } from '../types'
import { safeSetItem } from '../utils/safeStorage'
import { secureRandomId } from '../utils/secureId'

/** Singleton "view" tabs (opened from the + menu, one instance each). */
export type ViewKind = 'changes' | 'files' | 'artifacts' | 'subagents' | 'workflows' | 'logs' | 'side' | 'browser'
/** All tab kinds: singleton views + on-demand document/terminal tabs. */
export type TabKind = ViewKind | 'file' | 'diff' | 'artifact' | 'terminal'

/** Views that are AUTO-managed by content (see `syncPinned`): they appear —
 *  pinned to the front, non-closable, and absent from the + menu — only while
 *  they have content, and are removed when empty. Order here = strip order. */
export const PINNED_VIEWS: ViewKind[] = ['changes', 'files', 'artifacts']

export interface PanelTab {
  id: string
  kind: TabKind
  title: string
  /** Origin chat slot — comment submission routes to the session the tab was
   *  opened from, not whatever session is active later. */
  slot?: string | null
  // ── document fields ──
  path?: string
  content?: string
  original?: string
  modified?: string
  /** Last selected working-tree diff view for file tabs. Persisted with the
   *  tab so leaving and returning to a chat does not re-enable auto-diff. */
  diffMode?: boolean
  artifactSlug?: string
  artifactKind?: Artifact['kind']
  // ── terminal fields ──
  /** PTY session id — one live shell per terminal tab. */
  sessionId?: string
  /** Working directory the shell spawns in (the chat's project dir, if any). */
  cwd?: string
}

const VIEW_TITLES: Record<ViewKind, string> = {
  changes: 'Changes', files: 'Files', artifacts: 'Artifacts', subagents: 'Subagents', workflows: 'Workflows',
  logs: 'Logs', side: 'Side', browser: 'Browser',
}

/** Max concurrent terminal tabs per chat (each is a live PTY). At the cap,
 *  openTerminal focuses/reuses the most-recent terminal instead of spawning. */
export const MAX_TERMINALS_PER_CHAT = 4

const basename = (p: string) => p.split('/').pop() || p

type Bucket = { tabs: PanelTab[]; activeId: string | null }
type BySlot = Record<string, Bucket>
/** Module-level so an empty strip yields STABLE tabs/activeId identities
 *  (a per-render fallback object would churn the hook's memoized return). */
const EMPTY_BUCKET: Bucket = { tabs: [], activeId: null }

/* ── Module-level, persisted panel-tab store ──────────────────────────────
 * The strip must survive things that unmount ChatPage: activity-bar close,
 * activity-tab switches, chat switches, full route changes (ChatPage is a
 * route element), AND page reloads. Component-local useState died with the
 * route. So the per-slot buckets live here at module scope (read via
 * useSyncExternalStore) and are mirrored to localStorage. On reload the strip
 * is rehydrated; terminal tabs reconnect to the still-live PTY (backend orphan
 * window) and document tabs re-fetch their content lazily (see below). */

const KEY_PREFIX = 'mc-panel-tabs:'          // one key per slot: mc-panel-tabs:<slot>
const PERSIST_DEBOUNCE_MS = 300

let store: BySlot = loadPersisted()
const listeners = new Set<() => void>()

/* ── Inline file-preview drafts (keyed by absolute path) ───────────────────
 * The Files-tab inline editor's working copy lives HERE, at module scope, not
 * in a component's useState — so an in-progress edit survives everything that
 * unmounts the SidePanel subtree: the close control, an activity-tab switch,
 * a chat-slot switch, and the AUTOMATIC force-collapse when the window crosses
 * the width threshold. This mirrors how document-tab content persists above the
 * panel (in the buckets above). In-memory only (not localStorage): parity with
 * document tabs, whose heavy content is likewise stripped on persist and
 * re-fetched from disk on reload. Keyed by `slot::path` (an inline editor is
 * per chat slot, like the per-slot document tabs), so the SAME on-disk file
 * edited in two slots keeps independent drafts. A draft is cleared whenever
 * that slot's path is saved through ANY editor (see ChatPage.handleFileSave)
 * and on explicit discard, so a later inline reopen can't resurrect stale
 * content over a newer save. */
const inlineDrafts = new Map<string, string>()
// The store OWNS the draft key format (slot + path). Callers pass slot and path
// separately and never build the key themselves — a single owner prevents the
// four coordination sites (open / open-inline / save / slot-reset) from drifting
// on the key shape and silently reopening the data-loss bugs this closes.
const inlineDraftKey = (slot: string, path: string): string => `${slot}::${path}`
export function getInlineDraft(slot: string, path: string): string | undefined { return inlineDrafts.get(inlineDraftKey(slot, path)) }
export function setInlineDraft(slot: string, path: string, content: string): void { inlineDrafts.set(inlineDraftKey(slot, path), content) }
export function clearInlineDraft(slot: string, path: string): void { inlineDrafts.delete(inlineDraftKey(slot, path)) }

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}
function getSnapshot(): BySlot { return store }

/** Apply a transform to one slot's bucket, publish the new store, and persist.
 *  A new top-level object is created only on real change so useSyncExternalStore
 *  consumers re-render exactly when their store reference changes. */
function mutateSlot(key: string, fn: (b: Bucket) => Bucket): void {
  const prev = store[key] ?? { tabs: [], activeId: null }
  const nextBucket = fn(prev)
  if (nextBucket === prev) return
  store = { ...store, [key]: nextBucket }
  for (const cb of listeners) cb()
  schedulePersist(key)
}

/** Strip heavy bodies (file/diff/artifact content) before persisting — those
 *  can be MBs and blow the localStorage quota. Terminal + view tabs and all
 *  tab METADATA (path / slug / sessionId / cwd / order / focus) are kept, so
 *  document tabs restore as lightweight references and re-fetch their content
 *  on demand; artifact tabs self-hydrate by slug via ArtifactPanel's query. */
/** Lean single-bucket projection for persistence. Diff tabs are transient — a
 *  restored diff can only re-fetch the CURRENT working-tree diff, never the
 *  original turn snapshot, so it renders a misleading/unreliable diff; drop
 *  them (they still survive in-memory across in-app nav, where content is
 *  intact). Heavy content bodies are stripped (can be MBs). */
function serializeBucket(b: Bucket): string {
  const tabs = b.tabs
    .filter(t => t.kind !== 'diff')
    .map(t => { const copy = { ...t }; delete copy.content; return copy })
  // If the focused tab was a dropped diff tab, refocus a surviving tab.
  const activeId = tabs.some(t => t.id === b.activeId)
    ? b.activeId
    : (tabs.length ? tabs[tabs.length - 1].id : null)
  return JSON.stringify({ activeId, tabs })
}

function loadPersisted(): BySlot {
  if (typeof localStorage === 'undefined') return {}
  const out: BySlot = {}
  try {
    // Load every per-slot bucket (mc-panel-tabs:<slot>). Tolerate shape drift:
    // keep only well-formed buckets.
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (!k || !k.startsWith(KEY_PREFIX)) continue
      const slot = k.slice(KEY_PREFIX.length)
      if (!slot) continue
      try {
        const b = JSON.parse(localStorage.getItem(k) ?? 'null') as Partial<Bucket> | null
        if (b && Array.isArray(b.tabs)) {
          out[slot] = { tabs: b.tabs as PanelTab[], activeId: (b.activeId as string | null) ?? null }
        }
      } catch { /* skip malformed bucket */ }
    }
  } catch { /* enumerating storage can throw in locked-down envs */ }
  return out
}

let persistTimer: ReturnType<typeof setTimeout> | undefined
const dirtySlots = new Set<string>()
/** Persist only the slots that actually changed (one key each), debounced.
 *  Per-slot writes mean a GC'd slot key is never resurrected by an unrelated
 *  slot's mutation */
function schedulePersist(slot: string): void {
  if (typeof window === 'undefined') return
  dirtySlots.add(slot)
  clearTimeout(persistTimer)
  persistTimer = setTimeout(flushPersist, PERSIST_DEBOUNCE_MS)
}
function flushPersist(): void {
  for (const slot of dirtySlots) {
    const b = store[slot]
    if (b) safeSetItem(KEY_PREFIX + slot, serializeBucket(b))
    else if (typeof localStorage !== 'undefined') {
      try { localStorage.removeItem(KEY_PREFIX + slot) } catch { /* ignore */ }
    }
  }
  dirtySlots.clear()
}

/** Test-only: reset the module store (and its persisted copy) so each test
 *  starts from a clean strip — the module store otherwise leaks across the
 *  renderHook calls in a suite. */
export function __resetPanelTabs(): void {
  store = {}
  inlineDrafts.clear()
  clearTimeout(persistTimer)
  dirtySlots.clear()
  if (typeof localStorage !== 'undefined') {
    try {
      const doomed: string[] = []
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i)
        if (k && k.startsWith(KEY_PREFIX)) doomed.push(k)
      }
      for (const k of doomed) localStorage.removeItem(k)
    } catch { /* ignore */ }
  }
  for (const cb of listeners) cb()
}

/**
 * Tabbed side panel state. Replaces the old mutually-exclusive
 * usePanelState + useDiffPanel + activityTab model: every view (category views,
 * terminal) and every opened document (file / diff / artifact) is a tab in one
 * strip. Opening a document that's already open focuses its tab instead of
 * duplicating it. Content is held in the module store (not redux) to keep large
 * file bodies out of the store.
 *
 * State is bucketed PER CHAT SLOT (`slotKey`): each chat has its own strip
 * (tabs, order, focused tab), and switching chats swaps the whole strip —
 * switching back restores it exactly. Tabs opened with no active slot live in
 * a shared fallback bucket.
 *
 * The backing store is MODULE-LEVEL + localStorage-persisted (see above), so
 * the strip survives ChatPage unmounts (route changes) and page reloads. Only
 * tab metadata is persisted; document-tab content is re-fetched lazily by the
 * consumer after a reload (ChatPage's cold-tab hydration effect).
 */
export function usePanelTabs(slotKey: string | null = null) {
  const key = slotKey ?? '__no_slot__'
  const bySlot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const { tabs, activeId } = bySlot[key] ?? EMPTY_BUCKET

  /** Apply a bucket transform to the CURRENT slot's strip. */
  const update = useCallback((fn: (b: Bucket) => Bucket) => {
    mutateSlot(key, fn)
  }, [key])

  /** Add tab if its id is absent, otherwise merge patch into the existing tab;
   *  either way focus it. When `replaceId` is given (e.g. a file opened FROM
   *  the Files tab replaces that Files tab), the new tab takes the replaced
   *  tab's strip position; if the new tab already exists elsewhere, the
   *  replaced tab is simply closed. */
  const upsert = useCallback((tab: PanelTab, replaceId?: string) => {
    update(b => {
      const i = b.tabs.findIndex(t => t.id === tab.id)
      if (i !== -1) {
        const next = b.tabs.slice()
        next[i] = { ...next[i], ...tab }
        return { tabs: replaceId && replaceId !== tab.id ? next.filter(t => t.id !== replaceId) : next, activeId: tab.id }
      }
      if (replaceId) {
        const r = b.tabs.findIndex(t => t.id === replaceId)
        if (r !== -1) {
          const next = b.tabs.slice()
          next[r] = tab
          return { tabs: next, activeId: tab.id }
        }
      }
      return { tabs: [...b.tabs, tab], activeId: tab.id }
    })
  }, [update])

  const openView = useCallback((kind: ViewKind) => {
    upsert({ id: kind, kind, title: VIEW_TITLES[kind] })
  }, [upsert])

  /** Reconcile the AUTO-managed pinned views (Changes / Files / Artifacts) to
   *  exactly the ``available`` set: present-with-content ones are kept (or
   *  created), pinned to the FRONT in PINNED_VIEWS order; empty ones are
   *  removed. Dynamic tabs (documents / terminal / other views) keep their
   *  order after the pinned block. No-ops when already in the target shape so
   *  it's safe to call from a content-driven effect every render. */
  const syncPinned = useCallback((available: ViewKind[]) => {
    update(b => {
      const desired = PINNED_VIEWS.filter(k => available.includes(k))
      const dynamic = b.tabs.filter(t => !(PINNED_VIEWS as string[]).includes(t.id))
      const pinned = desired.map(
        k => b.tabs.find(t => t.id === k) ?? { id: k, kind: k, title: VIEW_TITLES[k] },
      )
      const nextTabs = [...pinned, ...dynamic]
      // Refocus if the active tab was a pinned view that just went away.
      const activeId = b.activeId && nextTabs.some(t => t.id === b.activeId)
        ? b.activeId
        : (nextTabs.length ? nextTabs[0].id : null)
      // Bail if nothing actually changed (id sequence + focus) — avoids churn.
      const sameOrder = nextTabs.length === b.tabs.length
        && nextTabs.every((t, i) => t.id === b.tabs[i].id)
      if (sameOrder && activeId === b.activeId) return b
      return { tabs: nextTabs, activeId }
    })
  }, [update])

  const openFile = useCallback((path: string, content: string, slot: string | null = null, opts?: { replaceId?: string }) => {
    upsert({ id: `file:${path}`, kind: 'file', title: basename(path), path, content, slot }, opts?.replaceId)
  }, [upsert])

  const openDiff = useCallback((path: string, modified: string, original = '') => {
    upsert({ id: `diff:${path}`, kind: 'diff', title: `${basename(path)} - Diff`, path, modified, original })
  }, [upsert])

  const openArtifact = useCallback((art: { slug: string; kind: Artifact['kind'] }, content: string, slot: string | null = null) => {
    upsert({ id: `artifact:${art.slug}`, kind: 'artifact', title: art.slug, artifactSlug: art.slug, artifactKind: art.kind, content, slot })
  }, [upsert])

  /** Patch fields on an existing tab WITHOUT focusing it (live content/query
   *  hydration — e.g. MarkdownPanel edits, artifact query resolving). */
  const patchTab = useCallback((id: string, patch: Partial<PanelTab>) => {
    update(b => {
      const i = b.tabs.findIndex(t => t.id === id)
      if (i === -1) return b
      const next = b.tabs.slice()
      next[i] = { ...next[i], ...patch }
      return { ...b, tabs: next }
    })
  }, [update])

  const closeTab = useCallback((id: string) => {
    update(b => {
      const i = b.tabs.findIndex(t => t.id === id)
      if (i === -1) return b
      const next = b.tabs.filter(t => t.id !== id)
      // Refocus a neighbor when closing the active tab (prefer the left one).
      const activeId = b.activeId !== id
        ? b.activeId
        : next.length === 0 ? null : (next[i - 1] ?? next[i] ?? next[next.length - 1]).id
      return { tabs: next, activeId }
    })
  }, [update])

  const closeAll = useCallback(() => { update(() => ({ tabs: [], activeId: null })) }, [update])

  const setActive = useCallback((id: string | null) => { update(b => ({ ...b, activeId: id })) }, [update])

  /** Replace the tab order wholesale (drag-to-reorder in the strip). */
  const setOrder = useCallback((next: PanelTab[]) => { update(b => ({ ...b, tabs: next })) }, [update])

  /** Open a NEW terminal tab (its own PTY session). Unlike singleton views,
   *  every call mints a fresh session so a chat can hold several shells; the
   *  per-slot bucketing makes those sessions chat-specific automatically. At
   *  the per-chat cap we focus (reuse) the most-recent terminal instead of
   *  spawning another. Returns the session id to connect / run against. */
  const openTerminal = useCallback((opts?: { cwd?: string }): string => {
    const terms = tabs.filter(t => t.kind === 'terminal')
    if (terms.length >= MAX_TERMINALS_PER_CHAT) {
      const last = terms[terms.length - 1]
      setActive(last.id)
      return last.sessionId ?? ''
    }
    // Cryptographically-strong id — a terminal session id is a security token
    // that addresses a live PTY, so it must not come from Math.random().
    // secureRandomId() uses crypto.randomUUID in a secure context and a
    // crypto.getRandomValues fallback when the dashboard is served over plain
    // HTTP from a non-loopback address (where randomUUID is undefined).
    const sessionId = secureRandomId()
    upsert({
      id: `terminal:${sessionId}`, kind: 'terminal',
      title: opts?.cwd ? basename(opts.cwd) : 'Terminal',
      sessionId, cwd: opts?.cwd,
    })
    return sessionId
  }, [tabs, upsert, setActive])

  /** Adopt an EXISTING terminal session as a tab in THIS slot (no new PTY) —
   *  the mirror of openTerminal, used to move a terminal from the app-wide
   *  bottom panel back into a chat. Reuses the given session id so its live
   *  shell + scrollback come along. Returns false at the per-chat cap. */
  const adoptTerminal = useCallback((sessionId: string, cwd?: string): boolean => {
    const id = `terminal:${sessionId}`
    if (tabs.some(t => t.id === id)) { setActive(id); return true }
    if (tabs.filter(t => t.kind === 'terminal').length >= MAX_TERMINALS_PER_CHAT) return false
    upsert({ id, kind: 'terminal', title: cwd ? basename(cwd) : 'Terminal', sessionId, cwd })
    return true
  }, [tabs, upsert, setActive])

  const activeTab = useMemo(() => tabs.find(t => t.id === activeId) ?? null, [tabs, activeId])

  return useMemo(() => ({
    tabs, activeId, activeTab,
    openView, openTerminal, adoptTerminal, openFile, openDiff, openArtifact,
    patchTab, closeTab, closeAll, setActive, setOrder, syncPinned,
    hasTabs: tabs.length > 0,
  }), [tabs, activeId, activeTab, openView, openTerminal, adoptTerminal, openFile, openDiff, openArtifact, patchTab, closeTab, closeAll, setActive, setOrder, syncPinned])
}
