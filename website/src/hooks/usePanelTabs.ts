import { useCallback, useMemo, useSyncExternalStore } from 'react'
import type { Artifact } from '../types'
import { i18nT } from '../i18n/t'
import { safeSetItem } from '../utils/safeStorage'
import { secureRandomId } from '../utils/secureId'

/** Singleton "view" tabs (opened from the + menu, one instance each). */
export type ViewKind = 'changes' | 'issues' | 'files' | 'artifacts' | 'subagents' | 'workflows' | 'logs' | 'context' | 'side' | 'browser'
/** All tab kinds: singleton views + on-demand document/terminal tabs. */
/** `app` hosts an MCP App (a sandboxed iframe with a live JSON-RPC bridge).
 *  It is deliberately a TabKind and NOT a ViewKind: SidePanel unmounts
 *  category views on tab switch (`if (!isActive) return null`), which would
 *  reload the app's iframe and destroy whatever the user has drawn. */
export type TabKind = ViewKind | 'file' | 'diff' | 'artifact' | 'terminal' | 'folder' | 'app'

/** Views that are AUTO-managed by content (see `syncPinned`): they appear —
 *  pinned to the front, non-closable, and absent from the + menu — only while
 *  they have content, and are removed when empty. Order here = strip order.
 *
 *  `issues` is deliberately NOT pinned: most sessions never mention an issue,
 *  so a permanent Issues tab would be an always-empty tab for the majority.
 *  It is opened on demand (from the + menu, or automatically by ChatPage when
 *  an issue url is first seen). */
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
  /**
   * A source line — or line RANGE — the panel should scroll to and flash, set
   * when the tab is opened from a `file.py:447` or `file.md:10-16` reference.
   * `endLine` is absent for a single-line citation.
   *
   * The `nonce` is what makes a repeat request act: re-clicking the same chip
   * produces the same `line`, which as a bare number would be `===` to the
   * previous value and re-trigger nothing.
   *
   * TRANSIENT — deliberately stripped in `serializeBucket`. A persisted line
   * would re-fire the jump on every page reload, days later, at a line number
   * the file may have long since outgrown.
   */
  revealLine?: { line: number; endLine?: number; nonce: number }
  artifactSlug?: string
  artifactKind?: Artifact['kind']
  // ── MCP App fields ──
  /** Tool-call id of the render this app tab hosts. Keyed the same way as
   *  `chat.mcpApps` (see `mcpAppKey`) so the body can select its payload. */
  appToolCallId?: string
  /** When this app tab was last focused (epoch ms). Drives warm-set eviction:
   *  the cap drops the LEAST-RECENTLY-USED frame, not the oldest-opened one, so
   *  a user who keeps returning to an early diagram does not have it evicted
   *  out from under them while newer renders stream in. */
  appActiveAt?: number
  // ── terminal fields ──
  /** PTY session id — one live shell per terminal tab. */
  sessionId?: string
  /** Working directory the shell spawns in (the chat's project dir, if any). */
  cwd?: string
}

/**
 * Catalog KEY for each singleton view's strip label.
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()`
 * call here would freeze the boot language and never re-resolve on a language
 * switch. Resolution happens in `viewTitle()` / `localiseTitles()`, which run
 * during render.
 *
 * Shaped as a flat `Record` of full literal keys, indexed inline at the
 * `i18nT()` call, because that is the form `scripts/check-i18n-keys.mjs` can
 * resolve statically.
 *
 * The tab ID is the `ViewKind` itself and is unaffected: it is compared,
 * persisted and rehydrated, so it must stay a stable identifier — only the
 * displayed title is localised.
 */
const VIEW_TITLE_KEY: Record<ViewKind, string> = {
  changes: 'hooks.usePanelTabs.changes',
  issues: 'hooks.usePanelTabs.issues',
  files: 'hooks.usePanelTabs.files',
  artifacts: 'hooks.usePanelTabs.artifacts',
  subagents: 'hooks.usePanelTabs.subagents',
  workflows: 'hooks.usePanelTabs.workflows',
  logs: 'hooks.usePanelTabs.logs',
  context: 'hooks.usePanelTabs.context',
  side: 'hooks.usePanelTabs.side',
  browser: 'hooks.usePanelTabs.browser',
}

/** Localised strip label for a singleton view. */
function viewTitle(kind: ViewKind): string {
  return i18nT(VIEW_TITLE_KEY[kind])
}

/**
 * Project stored tabs onto CURRENT-language titles.
 *
 * A view tab's title is DERIVED from its `kind`, so it is re-resolved on every
 * read rather than trusted from the store. Resolving only at open time would not
 * be enough: `title` is persisted (see `serializeBucket`), so a strip rehydrated
 * from localStorage — or one built before a language switch — would keep its
 * labels in the language they were opened in. Deriving also makes the round trip
 * through `setOrder`, which hands projected tabs back to the store, harmless.
 *
 * Document / terminal titles are real data (a basename, an artifact slug, a cwd)
 * and pass through untouched.
 *
 * `hasOwnProperty`, not `in`: a rehydrated tab's `kind` comes from localStorage,
 * so a persisted `kind: 'toString'` would otherwise resolve to an inherited
 * Object.prototype member and hand a function to i18next. Tabs whose title is
 * already correct keep their object identity, so consumers memoizing on a tab
 * don't churn.
 */
function localiseTitles(tabs: PanelTab[]): PanelTab[] {
  return tabs.map(tab => {
    if (!Object.prototype.hasOwnProperty.call(VIEW_TITLE_KEY, tab.kind)) return tab
    const title = viewTitle(tab.kind as ViewKind)
    return title === tab.title ? tab : { ...tab, title }
  })
}

/** Max concurrent MCP App tabs per chat (the "warm set").
 *
 *  Every app tab keeps a LIVE iframe mounted: SidePanel display-toggles app
 *  bodies instead of unmounting them, because a null-origin app frame cannot be
 *  remounted without reloading the app and losing the drawing. Each frame
 *  carries multi-MB of app HTML plus a running app runtime, so a session that
 *  renders a diagram per turn would otherwise accumulate one live frame per
 *  diagram for as long as the chat is open.
 *
 *  Terminals cap by REFUSING (`openTerminal` refocuses the newest instead of
 *  spawning) because one shell is as good as another. An app render is not
 *  fungible — the newest diagram is the one the user just asked for and must be
 *  shown — so apps cap by EVICTING the least-recently-used frame instead.
 *  Eviction is recoverable: the payload lives on in `chat.mcpApps` (bounded
 *  separately by MCP_APPS_PER_SLOT_MAX), so the chat bubble's side-panel control
 *  re-creates the tab and re-renders it. Only in-app edit state is lost. */
export const MAX_APP_TABS_PER_CHAT = 3

/** Max concurrent terminal tabs per chat (each is a live PTY). At the cap,
 *  openTerminal focuses/reuses the most-recent terminal instead of spawning. */
export const MAX_TERMINALS_PER_CHAT = 4

/** Monotonic id for reveal requests — see `PanelTab.revealLine`. Module-level so
 *  it is unique across slots and across tab identities, which is all the
 *  consumer's effect needs to tell one request from the next. */
let revealSeq = 0
const nextRevealNonce = (): number => ++revealSeq

/** Last path segment. Trailing slashes are stripped first: '/a/b/'.split('/')
 *  ends in '' which is falsy, so the naive form would fall back to the whole
 *  path and title a directory tab '/a/b/' instead of 'b'. */
const basename = (p: string) => p.replace(/\/+$/, '').split('/').pop() || p

type Bucket = { tabs: PanelTab[]; activeId: string | null }
type BySlot = Record<string, Bucket>
/** Module-level so an empty strip yields STABLE tabs/activeId identities
 *  (a per-render fallback object would churn the hook's memoized return). */
const EMPTY_BUCKET: Bucket = { tabs: [], activeId: null }

/* ── Module-level, persisted panel-tab store ──────────────────────────────
 * The strip must survive things that unmount ChatPage: activity-bar close,
 * activity-tab switches, chat switches, full route changes (ChatPage is a
 * route element), AND page reloads. Component-local useState would not survive
 * that, so the per-slot buckets live here at module scope (read via
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

/* ── Auto-open bookkeeping for MCP App tabs ───────────────────────────────
 * Which (slot, tool-call) renders the auto-open effect has ALREADY acted on.
 *
 * Module scope, not a component ref, for the same reason the buckets above are:
 * a `useRef` in ChatPage is recreated on every ChatPage mount, so navigating to
 * Settings and back re-armed the effect and it re-opened — and re-focused — a
 * tab the user had deliberately closed. Keyed by slot + tool-call id so the same
 * render in two slots is tracked independently.
 *
 * In-memory only. A full page reload legitimately re-arms auto-open: the tab
 * strip does not persist app tabs (see `serializeBucket`), so nothing would
 * re-open the panel otherwise.
 *
 * Nested rather than a composite `slot|id` string key: no separator to collide
 * with, and no string-building that reads like user copy to the i18n gate. */
const autoOpenedApps = new Map<string, Set<string>>()

/** Claim the one auto-open this (slot, tool-call) render is allowed. Returns
 *  true exactly once per pair — the caller opens the tab only on a true. */
export function claimAppAutoOpen(slot: string, toolCallId: string): boolean {
  let seen = autoOpenedApps.get(slot)
  if (!seen) { seen = new Set<string>(); autoOpenedApps.set(slot, seen) }
  if (seen.has(toolCallId)) return false
  seen.add(toolCallId)
  return true
}

/** Test seam: forget every auto-open claim. */
export function __resetAppAutoOpen(): void { autoOpenedApps.clear() }
// The store OWNS the draft key format (slot + path). Callers pass slot and path
// separately and never build the key themselves — a single owner prevents the
// four coordination sites (open / open-inline / save / slot-reset) from drifting
// on the key shape, which would silently reintroduce data-loss bugs.
const inlineDraftKey = (slot: string, path: string): string => `${slot}::${path}`
export function getInlineDraft(slot: string, path: string): string | undefined { return inlineDrafts.get(inlineDraftKey(slot, path)) }
export function setInlineDraft(slot: string, path: string, content: string): void { inlineDrafts.set(inlineDraftKey(slot, path), content) }
export function clearInlineDraft(slot: string, path: string): void { inlineDrafts.delete(inlineDraftKey(slot, path)) }

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** EVERY live app tab, across every slot, in a stable order.
 *
 *  SidePanel renders all of them from this ONE list with each tab's own id as
 *  the React key, and toggles visibility — so an app frame keeps its identity
 *  when the active chat changes. An earlier version rendered the active slot's
 *  tab through the normal tab loop and other slots' through a second loop with a
 *  `bg:`-prefixed key; switching chats moved a tab between the two lists, the key
 *  changed, and React remounted the very iframe the split existed to preserve.
 *
 *  Ordered by slot then insertion so the list does not reshuffle between renders
 *  (a reorder would not remount, but it makes the DOM churn for no reason).
 *
 *  Bounded by the per-slot warm cap (MAX_APP_TABS_PER_CHAT) times the number of
 *  slots holding app tabs — under the per-slot payload cap that already governs
 *  how many frames can be live at once. */
export function useAllAppTabs(): PanelTab[] {
  const bySlot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return useMemo(() => {
    const out: PanelTab[] = []
    for (const slot of Object.keys(bySlot).sort()) {
      for (const t of bySlot[slot].tabs) if (t.kind === 'app') out.push(t)
    }
    return out
  }, [bySlot])
}

/** Whether ANY slot holds a live app tab.
 *
 *  The mount guard must consult every slot, not just the active one: with
 *  cross-slot hosting, a frame belonging to chat A lives in the panel subtree
 *  while chat B is active, so deciding to unmount on B's (empty) tab list would
 *  destroy A's canvas. */
export function useAnyLiveAppTab(): boolean {
  const bySlot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  return useMemo(
    () => Object.values(bySlot).some(b => b.tabs.some(t => t.kind === 'app')),
    [bySlot],
  )
}
function getSnapshot(): BySlot { return store }

/** Bucket key for "tabs opened while no chat is active". */
const NO_SLOT_KEY = '__no_slot__'
const bucketKey = (slotKey: string | null): string => slotKey ?? NO_SLOT_KEY

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

/** Add tab if its id is absent, otherwise merge patch into the existing tab;
 *  either way focus it. When `replaceId` is given (e.g. a file opened FROM the
 *  Files tab replaces that Files tab), the new tab takes the replaced tab's
 *  strip position; if the new tab already exists elsewhere, the replaced tab is
 *  simply closed.
 *
 *  Module-level (not a hook callback) because two callers need it: the bound
 *  `upsert` below, and `openPanelView`, which addresses a slot EXPLICITLY. */
function upsertInBucket(b: Bucket, tab: PanelTab, replaceId?: string): Bucket {
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
}

/** Open (and focus) a singleton view tab in a SPECIFIC slot's strip, with no
 *  hook binding.
 *
 *  The sidebar asks for a panel view on a chat that is not active yet — clicking
 *  a session row's PR chip switches sessions and opens Changes in one gesture.
 *  `usePanelTabs` is bound to whichever slot was active when it rendered, so
 *  going through `openView` there would open the tab on the chat being LEFT. */
export function openPanelView(slotKey: string | null, kind: ViewKind): void {
  mutateSlot(bucketKey(slotKey), b => upsertInBucket(b, { id: kind, kind, title: viewTitle(kind) }))
}

/** Strip heavy bodies (file/diff/artifact content) before persisting — those
 *  can be MBs and blow the localStorage quota. Terminal + view tabs and all
 *  tab METADATA (path / slug / sessionId / cwd / order / focus) are kept, so
 *  document tabs restore as lightweight references and re-fetch their content
 *  on demand; artifact tabs self-hydrate by slug via ArtifactPanel's query. */
/** Lean single-bucket projection for persistence. Diff and app tabs are
 *  transient — a restored diff can only re-fetch the CURRENT working-tree diff,
 *  never the original turn snapshot, so it renders a misleading/unreliable diff;
 *  an MCP App tab is worse still, because its render payload arrives ONLY on a
 *  live `mcp_app_render` event and is never persisted, so a rehydrated app tab
 *  could never show anything at all. Drop both (they still survive in-memory
 *  across in-app nav, where content is intact). Heavy content bodies are
 *  stripped (can be MBs). */
function serializeBucket(b: Bucket): string {
  const tabs = b.tabs
    .filter(t => t.kind !== 'diff' && t.kind !== 'app')
    .map(t => { const copy = { ...t }; delete copy.content; delete copy.revealLine; return copy })
  // If the focused tab was a dropped diff/app tab, refocus a surviving tab.
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
  autoOpenedApps.clear()
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
 * Tabbed side panel state: every view (category views, terminal) and every
 * opened document (file / diff / artifact) is a tab in one strip. Opening a document that's already open focuses its tab instead of
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
  const key = bucketKey(slotKey)
  const bySlot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
  const { tabs: storedTabs, activeId } = bySlot[key] ?? EMPTY_BUCKET
  // View-tab labels are re-resolved from `kind` on every read so the strip is in
  // the CURRENT language — see `localiseTitles`. Memoized on the stored array so
  // an unchanged strip keeps a stable `tabs` identity.
  const tabs = useMemo(() => localiseTitles(storedTabs), [storedTabs])

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
    update(b => upsertInBucket(b, tab, replaceId))
  }, [update])

  const openView = useCallback((kind: ViewKind) => {
    upsert({ id: kind, kind, title: viewTitle(kind) })
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
        k => b.tabs.find(t => t.id === k) ?? { id: k, kind: k, title: viewTitle(k) },
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

  const openFile = useCallback((path: string, content: string, slot: string | null = null, opts?: { replaceId?: string; line?: number; endLine?: number }) => {
    // `revealLine` is always present in the object, `undefined` when absent:
    // `upsert` merges onto an existing tab with a spread, which only overwrites
    // keys the incoming object HAS. Omitting it would leave a previous chip's
    // line on the tab, so a later plain click on the same file would re-jump to
    // a line the user did not ask for.
    upsert({
      id: `file:${path}`, kind: 'file', title: basename(path), path, content, slot,
      revealLine: opts?.line != null ? { line: opts.line, endLine: opts.endLine, nonce: nextRevealNonce() } : undefined,
    }, opts?.replaceId)
  }, [upsert])

  const openDiff = useCallback((path: string, modified: string, original = '') => {
    upsert({ id: `diff:${path}`, kind: 'diff', title: i18nT('hooks.usePanelTabs.diff', { name: basename(path) }), path, modified, original })
  }, [upsert])

  /** Open a directory listing as its own tab. Keyed `folder:${path}` so a
   *  directory and a same-named file never collide on id, and so re-opening the
   *  same directory focuses the existing tab instead of stacking duplicates. */
  const openFolder = useCallback((path: string, slot: string | null = null) => {
    upsert({ id: `folder:${path}`, kind: 'folder', title: basename(path), path, slot })
  }, [upsert])

  /** Open (or refocus) the app tab hosting one MCP App render. Keyed by
   *  tool-call id, so a re-render of the same app reuses its tab instead of
   *  stacking duplicates.
   *
   *  Bounded by MAX_APP_TABS_PER_CHAT with least-recently-used eviction. The cap
   *  runs INSIDE `update` rather than against the `tabs` closure — unlike
   *  `openTerminal`, this is called from an effect that loops over every pending
   *  render, so two same-tick opens would both read a stale pre-insert `tabs` and
   *  each conclude there was room. The currently-focused tab is never a candidate
   *  (belt-and-braces: focus stamping already sorts it last). */
  const openApp = useCallback((toolCallId: string, title: string, slot: string | null = null) => {
    const id = `app:${toolCallId}`
    update(b => {
      const now = Date.now()
      const i = b.tabs.findIndex(t => t.id === id)
      if (i !== -1) {
        const next = b.tabs.slice()
        next[i] = { ...next[i], title, appToolCallId: toolCallId, slot, appActiveAt: now }
        return { tabs: next, activeId: id }
      }
      let kept = b.tabs
      // Count EVERY app tab toward the budget (including the focused one), but
      // only ever evict from the unfocused ones.
      //
      // There is deliberately NO "spare the tabs the user worked in" exemption. An
      // earlier version exempted tabs marked visited, which protected the WRONG set:
      // auto-open focuses a tab without marking it visited, so the frame a user is
      // most likely to draw in — the one that just appeared — was the first evicted,
      // while a tab they merely clicked and left was kept. A null-origin sandboxed
      // frame cannot be asked whether its canvas is dirty, so no proxy for that is
      // available; a bound that is honest about evicting beats a heuristic that
      // claims to protect work and does not. Eviction stays recoverable: the payload
      // survives in `chat.mcpApps`, so the chat bubble's control rebuilds the frame.
      const allApps = kept.filter(t => t.kind === 'app')
      const need = allApps.length + 1 - MAX_APP_TABS_PER_CHAT
      if (need > 0) {
        // `slice(0, need)` stops short when there are not enough discardable
        // frames, leaving the set temporarily over the cap rather than throwing
        // away work. That is bounded anyway: a frame unmounts once its payload
        // is evicted, and payloads are already capped by MCP_APPS_PER_SLOT_MAX.
        const doomed = new Set(
          allApps
            .filter(t => t.id !== b.activeId)
            .sort((x, y) => (x.appActiveAt ?? 0) - (y.appActiveAt ?? 0))
            .slice(0, need)
            .map(t => t.id),
        )
        kept = kept.filter(t => !doomed.has(t.id))
      }
      const tab: PanelTab = { id, kind: 'app', title, appToolCallId: toolCallId, slot, appActiveAt: now }
      return { tabs: [...kept, tab], activeId: id }
    })
  }, [update])

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

  /** Focus a tab. Focusing an app tab stamps `appActiveAt` so the warm-set cap
   *  evicts by least-recent USE rather than by open order. Other kinds take the
   *  identity-preserving path so consumers memoizing on a tab don't churn. */
  const setActive = useCallback((id: string | null) => {
    update(b => {
      const i = id ? b.tabs.findIndex(t => t.id === id) : -1
      if (i === -1 || b.tabs[i].kind !== 'app') return { ...b, activeId: id }
      const next = b.tabs.slice()
      next[i] = { ...next[i], appActiveAt: Date.now() }
      return { tabs: next, activeId: id }
    })
  }, [update])

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
    openView, openTerminal, adoptTerminal, openFile, openDiff, openArtifact, openFolder, openApp,
    patchTab, closeTab, closeAll, setActive, setOrder, syncPinned,
    hasTabs: tabs.length > 0,
  }), [tabs, activeId, activeTab, openView, openTerminal, adoptTerminal, openFile, openDiff, openArtifact, openFolder, openApp, patchTab, closeTab, closeAll, setActive, setOrder, syncPinned])
}
