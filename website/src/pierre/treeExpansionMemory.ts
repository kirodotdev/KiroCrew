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
 * access goes through `utils/safeStorage`, so it is best-effort but not
 * defeatist: a FULL quota reclaims a tier of disposable cache and retries
 * rather than losing the write, while storage that is unusable at all
 * (private mode) degrades to session-only memory and never to a crash — the
 * same tolerance the rail width code has for a bad stored value.
 */

import { safeGetItem, safeSetItem } from '../utils/safeStorage'

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

/** A bounded, per-project set of tree paths remembered across the rail's
 *  remount (module-scope map) and a page reload (`localStorage` mirror under
 *  `storageKey`). */
export interface ProjectPathMemory {
  /** Remember `paths` for `projectDir`, replacing what was remembered. */
  remember(projectDir: string, paths: readonly string[]): void
  /** The remembered paths for `projectDir`: session map first, then
   *  `localStorage`, then empty. */
  recall(projectDir: string): readonly string[]
  /** Test-only: drop the module-scope session map so suites stay isolated. */
  __resetForTests(): void
}

/** Build one memory over its own storage key. The expansion memory below is
 *  one instance; the dismissed "Folders not readable" notices
 *  (`./treeUnreadableDismissals`) are another — the same session map, mirror,
 *  caps and tolerance for unusable storage, spelled once. */
export function createProjectPathMemory(storageKey: string): ProjectPathMemory {
  /** Paths for the current page session. Module-level so they survive the
   *  rail's remount on in-place tab navigation. Bounded like the stored
   *  record: a long-lived tab must not retain an entry per project dir ever
   *  visited. */
  const session = new Map<string, readonly string[]>()

  /** Delete-then-set keeps insertion order meaning "least recently written
   *  first"; evicting the first key past the cap then drops the stalest. */
  const setBounded = (key: string, value: readonly string[]): void => {
    session.delete(key)
    session.set(key, value)
    for (const k of session.keys()) {
      if (session.size <= MAX_REMEMBERED_DIRS) break
      session.delete(k)
    }
  }

  /** Read the whole stored record, or `{}` for anything unusable — missing,
   *  unreadable, or not a JSON object.
   *
   *  There is deliberately no "storage is broken, skip the write" channel. It
   *  existed to avoid a write that could only fail, and `safeSetItem` already
   *  makes such a write harmless. Keeping it cost more than it saved: a value
   *  that throws in `JSON.parse` took the same branch, so ONE corrupt string
   *  blocked every later write and the mirror stayed dead for the life of the
   *  profile. Falling back to `{}` overwrites the bad value on the next
   *  expansion instead. */
  const readStore = (): Record<string, unknown> => {
    const raw = safeGetItem(storageKey)
    if (!raw) return {}
    try {
      const parsed: unknown = JSON.parse(raw)
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
      return parsed as Record<string, unknown>
    } catch {
      return {}
    }
  }

  return {
    // The mirror write serializes the whole record synchronously. Accepted
    // deliberately: callers only invoke this on a REAL change (user-paced
    // clicks, deduped upstream), and one JSON.stringify of a 20x500-capped
    // record is far below frame budget on those. Deferring the write would add
    // a lost-on-navigation window for no felt gain.
    remember(projectDir, paths) {
      const capped = paths.slice(0, MAX_REMEMBERED_PATHS)
      setBounded(projectDir, capped)
      const store = readStore()
      // Delete-then-set keeps insertion order meaning "least recently written
      // first", so eviction below always drops the stalest project.
      delete store[projectDir]
      store[projectDir] = capped
      const dirs = Object.keys(store)
      for (let i = 0; i < dirs.length - MAX_REMEMBERED_DIRS; i++) delete store[dirs[i]]
      // `safeSetItem`, not a raw write in a bare catch: on a full origin quota it
      // drops a tier of disposable cache and retries, where the bare catch lost
      // the write outright. That quota is reached in normal use — the per-session
      // virtualizer height caches accumulate across every chat ever opened — so
      // the bare catch meant the mirror quietly stopped persisting while several
      // MB of re-derivable cache sat next to it. It still never throws: a
      // private-mode SecurityError is not reclaimable, returns false, and leaves
      // the session map above covering the remount case.
      safeSetItem(storageKey, JSON.stringify(store))
    },
    recall(projectDir) {
      const inSession = session.get(projectDir)
      if (inSession) return inSession
      const store = readStore()
      const entry = store[projectDir]
      if (!Array.isArray(entry)) return []
      const paths = entry
        .filter((p): p is string => typeof p === 'string')
        .slice(0, MAX_REMEMBERED_PATHS)
      setBounded(projectDir, paths)
      return paths
    },
    __resetForTests() {
      session.clear()
    },
  }
}

const expansion = createProjectPathMemory(STORAGE_KEY)

/** Remember the expanded directory set for a project directory. */
export function rememberExpandedPaths(projectDir: string, expanded: readonly string[]): void {
  expansion.remember(projectDir, expanded)
}

/** Recall the remembered expanded directory set for a project directory:
 *  session map first, then `localStorage`, then empty. */
export function recallExpandedPaths(projectDir: string): readonly string[] {
  return expansion.recall(projectDir)
}

/** Test-only: drop the module-scope session map so suites stay isolated. */
export function __resetTreeExpansionMemoryForTests(): void {
  expansion.__resetForTests()
}
