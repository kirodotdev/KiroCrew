/**
 * Remembered expansion state for the workspace file tree, keyed by project
 * directory.
 *
 * The Files tab mounts only while active, so in-place tab navigation (opening
 * a file focuses the file's tab) unmounts and remounts the whole tree, and the
 * tree model is created per mount. Sibling rail state already survives that
 * remount — the All/Changed mode through a module-scope variable, the rail
 * width through `localStorage`. This module is the expansion's equivalent:
 * a module-scope map for the page session, mirrored to `localStorage` so a
 * page reload keeps it too — without it, every remount starts fully
 * collapsed and every file open costs re-expanding the path from the root.
 *
 * Paths are the project-relative POSIX directory paths the tree model itself
 * speaks (`resetPaths` input, and `FileTreeVisibleRow.path` with the library's
 * trailing directory slash stripped), so a `project-tree` refetch that
 * rebuilds the snapshot with fresh node ids cannot invalidate them. Storage
 * access is best-effort: private mode or a full quota degrades to
 * session-only memory, never to a crash — the same tolerance the rail width
 * code has for a bad stored value.
 */

/** One storage key for every project directory, holding a `{ dir: paths }`
 *  record in least-recently-written-first insertion order. A key per project
 *  dir would grow the origin's quota use without bound; one record with an
 *  LRU cap keeps the footprint fixed. */
const STORAGE_KEY = 'mc-files-tree-expanded'

/** Cap on remembered project directories: the least recently written entry is
 *  evicted past this, so an old project only costs its restored state. */
const MAX_REMEMBERED_DIRS = 20

/** Cap on remembered paths per project: a pathological workspace with
 *  thousands of expanded directories must not bloat `localStorage`. Dropping
 *  the tail only costs those directories their restored state. */
const MAX_REMEMBERED_PATHS = 500

/** Expansion for the current page session. Module-level so it survives the
 *  rail's remount on in-place tab navigation. Bounded like the stored record:
 *  a long-lived tab must not retain an entry per project dir ever visited. */
const sessionExpansion = new Map<string, readonly string[]>()

/** Delete-then-set keeps insertion order meaning "least recently written
 *  first"; evicting the first key past the cap then drops the stalest. */
function setBounded(map: Map<string, readonly string[]>, key: string, value: readonly string[]): void {
  map.delete(key)
  map.set(key, value)
  for (const k of map.keys()) {
    if (map.size <= MAX_REMEMBERED_DIRS) break
    map.delete(k)
  }
}

/** Read the whole stored record. `{}` for a missing or malformed value;
 *  `null` when storage itself is unusable (so a write should not follow). */
function readStore(): Record<string, unknown> | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return parsed as Record<string, unknown>
  } catch {
    return null
  }
}

/** Remember the expanded directory set for a project directory.
 *
 *  The mirror write serializes the whole record synchronously. Accepted
 *  deliberately: the caller only invokes this on a REAL expansion change
 *  (user-paced clicks, deduped upstream), and one JSON.stringify of a
 *  20x500-capped record is far below frame budget on those. Deferring the
 *  write would add a lost-on-navigation window for no felt gain. */
export function rememberExpandedPaths(projectDir: string, expanded: readonly string[]): void {
  const capped = expanded.slice(0, MAX_REMEMBERED_PATHS)
  setBounded(sessionExpansion, projectDir, capped)
  const store = readStore()
  if (store === null) return
  // Delete-then-set keeps insertion order meaning "least recently written
  // first", so eviction below always drops the stalest project.
  delete store[projectDir]
  store[projectDir] = capped
  const dirs = Object.keys(store)
  for (let i = 0; i < dirs.length - MAX_REMEMBERED_DIRS; i++) delete store[dirs[i]]
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store))
  } catch {
    // Private mode / quota exceeded: the session map above still covers the
    // remount case, only reload persistence is lost.
  }
}

/** Recall the remembered expanded directory set for a project directory:
 *  session map first, then `localStorage`, then empty. */
export function recallExpandedPaths(projectDir: string): readonly string[] {
  const inSession = sessionExpansion.get(projectDir)
  if (inSession) return inSession
  const store = readStore()
  const entry = store?.[projectDir]
  if (!Array.isArray(entry)) return []
  const paths = entry
    .filter((p): p is string => typeof p === 'string')
    .slice(0, MAX_REMEMBERED_PATHS)
  setBounded(sessionExpansion, projectDir, paths)
  return paths
}

/** Test-only: drop the module-scope session map so suites stay isolated. */
export function __resetTreeExpansionMemoryForTests(): void {
  sessionExpansion.clear()
}
