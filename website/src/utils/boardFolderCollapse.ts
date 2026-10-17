import { safeGetItem, safeRemoveItem, safeSetItem } from './safeStorage'

/** Per-column collapse overrides for board-view session folders.
 *
 *  A folder's `collapsed` field is a single server-persisted flag shared by
 *  every surface that renders the folder. In board view the same root folders
 *  render once per column, so acting on the shared flag from one column would
 *  collapse the folder everywhere. These overrides give each (column, folder)
 *  pair its own client-local expansion state, layered over the server flag:
 *  a column with no recorded override follows the server default.
 *
 *  Each override persists under its OWN localStorage key. Per-key storage is
 *  what makes cross-tab loss unrepresentable: two tabs writing different
 *  overrides touch different keys, and two tabs writing the SAME override
 *  converge on a last-writer-wins value for that one pair — there is no shared
 *  blob whose read-modify-write could interleave and drop the other tab's
 *  entry. Reads/writes go through safeStorage (quota reclaim + retry), the
 *  repo's single guarded entry point for Web Storage.
 *
 *  The host backup (`lib/uiPrefs.ts`) holds the whole family as ONE durable
 *  key, `kc-board-folder-collapsed`. The host caps the number of keys it
 *  stores, so the pairs cannot travel one key each. That key exists only on
 *  the wire. `boardFolderCollapseBackup` projects it from the per-key entries
 *  and writes nothing. A host restore hands its value to
 *  `adoptBoardFolderCollapse`, which writes the pairs it lists as per-key
 *  entries and removes the rest, so the key itself never reaches localStorage.
 */

const KEY_PREFIX = 'kc-board-folder-collapsed:'

export function boardCollapseKey(columnId: string, folderId: string): string {
  return `${columnId}:${folderId}`
}

function storageKey(overrideKey: string): string {
  return KEY_PREFIX + overrideKey
}

/** The column a board droppable id belongs to, or null for non-board targets.
 *  Board folder droppables are `col-<columnId>-folder-drop:<folderId>`; the
 *  list view's droppables carry no `col-` prefix, so they parse to null and
 *  callers fall back to the server flag. */
export function boardColumnFromDroppableId(droppableId: string): string | null {
  const m = /^col-(.*)-folder-drop:/.exec(droppableId)
  return m ? m[1] : null
}

/** Enumerate this module's storage keys. Iteration needs its own guard:
 *  safeStorage covers get/set, but touching `localStorage` at all throws in
 *  storage-blocked contexts, and that must degrade to "no overrides" rather
 *  than break the sidebar render. */
function overrideStorageKeys(): string[] {
  const keys: string[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key && key.startsWith(KEY_PREFIX)) keys.push(key)
    }
  } catch { /* storage unavailable -> no overrides */ }
  return keys
}

export function loadBoardFolderCollapse(): Map<string, boolean> {
  const overrides = new Map<string, boolean>()
  for (const key of overrideStorageKeys()) {
    const value = safeGetItem(key)
    // Anything but the two written values is a foreign/corrupt entry — skip
    // it rather than guessing, and leave the rest of the map intact.
    if (value === '1') overrides.set(key.slice(KEY_PREFIX.length), true)
    else if (value === '0') overrides.set(key.slice(KEY_PREFIX.length), false)
  }
  return overrides
}

/** The family as the host backup stores it: one JSON object with the pairs in
 *  sorted order. The same overrides therefore always produce the same string.
 *  A round trip through the host reads as unchanged. */
function serializeFamily(overrides: Map<string, boolean>): string {
  const out: Record<string, boolean> = {}
  for (const pair of [...overrides.keys()].sort()) out[pair] = overrides.get(pair)!
  return JSON.stringify(out)
}

/** The value the host backup stores for the family. Null when there is no
 *  override to back up. Read-only: it writes nothing. */
export function boardFolderCollapseBackup(): string | null {
  const overrides = loadBoardFolderCollapse()
  return overrides.size === 0 ? null : serializeFamily(overrides)
}

/** The pairs a host backup value lists, as `pair -> collapsed`. An entry is
 *  skipped unless its value is a boolean and its key contains a `:`. Empty
 *  when the value is not a JSON object. */
function parseFamily(raw: string): Map<string, boolean> {
  const family = new Map<string, boolean>()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const [pair, collapsed] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof collapsed === 'boolean' && pair.includes(':')) family.set(pair, collapsed)
      }
    }
  } catch { /* not JSON: nothing to adopt */ }
  return family
}

/** Writes a host backup value as the whole family. Every pair it lists becomes
 *  its own per-key entry. Every entry it does not list is removed. Returns
 *  false when the value lists no usable pair. Also returns false when the
 *  quota-safe writer refuses a write. On a refusal the family reads as it did
 *  before the call. The entries this call added are removed. The entries it
 *  overwrote are written back. An overwritten entry the writer also refuses
 *  to write back is removed instead. A quota refusal needs a value longer than
 *  the one byte written over it. The read skips every such value as corrupt.
 *  Idempotent. */
export function adoptBoardFolderCollapse(raw: string): boolean {
  const family = parseFamily(raw)
  if (family.size === 0) return false
  const previous = new Map<string, string | null>()
  for (const [pair, collapsed] of family) {
    const key = storageKey(pair)
    const value = collapsed ? '1' : '0'
    const before = safeGetItem(key)
    if (before === value) continue
    if (!safeSetItem(key, value)) {
      for (const [k, v] of previous) {
        if (v === null || !safeSetItem(k, v)) safeRemoveItem(k)
      }
      return false
    }
    previous.set(key, before)
  }
  for (const key of overrideStorageKeys()) {
    if (!family.has(key.slice(KEY_PREFIX.length))) safeRemoveItem(key)
  }
  return true
}

export function persistBoardOverride(columnId: string, folderId: string, collapsed: boolean): void {
  safeSetItem(storageKey(boardCollapseKey(columnId, folderId)), collapsed ? '1' : '0')
}

/** Persisted twin of clearFolderOverrides — same collapsed-only rule. */
export function persistClearFolderOverrides(folderId: string, columnId?: string): void {
  for (const key of overrideStorageKeys()) {
    const overrideKey = key.slice(KEY_PREFIX.length)
    if (!matchesClear(overrideKey, folderId, columnId)) continue
    if (safeGetItem(key) !== '1') continue
    safeRemoveItem(key)
  }
}

function matchesClear(overrideKey: string, folderId: string, columnId?: string): boolean {
  if (!overrideKey.endsWith(`:${folderId}`)) return false
  return columnId === undefined || overrideKey === boardCollapseKey(columnId, folderId)
}

/** Drop collapsed overrides for one folder — every column's by default, or a
 *  single column's when `columnId` is given. Used when a programmatic
 *  expansion (reveal, create-in-folder, drag-hover auto-expand) must win over
 *  a column's local collapsed state. Only COLLAPSED overrides block an
 *  expansion, so only those are cleared: an expand override (false) already
 *  shows the folder open, and deleting it would hand the column back to the
 *  server flag — if the server-side expansion then fails and rolls back, the
 *  folder would unexpectedly collapse. */
export function clearFolderOverrides(overrides: Map<string, boolean>, folderId: string, columnId?: string): Map<string, boolean> {
  const keys: string[] = []
  for (const [key, value] of overrides) {
    if (value === true && matchesClear(key, folderId, columnId)) keys.push(key)
  }
  if (keys.length === 0) return overrides
  const next = new Map(overrides)
  for (const key of keys) next.delete(key)
  return next
}
