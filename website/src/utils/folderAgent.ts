import type { ChatFolder } from '../types'
import { ApiError } from '../api/apiError'

/**
 * First non-empty `pick(folder)` on the named folder or, failing that, on the
 * closest ancestor that sets it. `undefined` when nothing on the chain does.
 *
 * Cycle-guarded, because `parent_id` is user-editable and a corrupt chain would
 * otherwise spin: a folder revisited on the way up ends the walk. A `parent_id`
 * naming a folder that no longer exists ends it too.
 */
function nearestFolderValue(
  folders: ChatFolder[],
  folderId: string,
  pick: (folder: ChatFolder) => string | undefined
): string | undefined {
  let current: ChatFolder | undefined = folders.find(f => f.id === folderId)
  const seen = new Set<string>()
  while (current) {
    if (seen.has(current.id)) break // cycle guard
    seen.add(current.id)
    const value = pick(current)
    if (value) return value
    const parentId = current.parent_id
    current = parentId ? folders.find(f => f.id === parentId) : undefined
  }
  return undefined
}

/**
 * Resolve which agent to use when creating a session in a folder, walking up
 * the folder hierarchy the same way `resolveFolderProjectDir` does.
 * Priority: nearest ancestor's default_agent → globalDefaultAgent → undefined
 *
 * An empty `default_agent` on a subfolder means "inherit", not "use the global
 * default": a subfolder of an agent-pinned folder would otherwise silently run
 * the global default, and the pin would have to be repeated on every level.
 */
export function resolveFolderAgent(
  folders: ChatFolder[],
  folderId: string,
  globalDefaultAgent: string
): string | undefined {
  return nearestFolderValue(folders, folderId, f => f.default_agent)
    || globalDefaultAgent
    || undefined
}

/**
 * Resolve project_dir by walking up the folder hierarchy.
 * Returns the nearest ancestor's project_dir, or undefined.
 */
export function resolveFolderProjectDir(
  folders: ChatFolder[],
  folderId: string
): string | undefined {
  return nearestFolderValue(folders, folderId, f => f.project_dir)
}

/**
 * The folder's resolved project_dir when it differs from the session's own
 * `project`, else undefined. No folder, or a chain that sets no directory, is
 * never a mismatch. Both sides are realpath-normalized by the gateway, so a
 * plain comparison is exact.
 */
export function folderProjectMismatch(
  folders: ChatFolder[],
  folderId: string | undefined,
  project: string | undefined
): string | undefined {
  if (!folderId) return undefined
  const dir = resolveFolderProjectDir(folders, folderId)
  return dir && dir !== (project || '') ? dir : undefined
}

/**
 * Resolve the extra steering directories for a folder.
 *
 * Unlike `resolveFolderProjectDir` (nearest-wins), steering dirs ACCUMULATE up
 * the parent_id chain: an org-standards folder above a per-repo folder must
 * contribute both sets. The result is ordered ROOT-FIRST — the outermost
 * ancestor's dirs precede the folder's own — and de-duplicated, keeping the
 * FIRST occurrence so the root-most contribution wins its position.
 *
 * Cycle-guarded like `nearestFolderValue`: a corrupt `parent_id` chain (a
 * folder revisited on the way up, or a parent that no longer exists) ends the
 * walk rather than spinning.
 *
 * PRINCIPAL-FILTERED like the backend delivery gate: a level owned by another
 * principal (a non-empty `owner_app` that differs from `principal`) is skipped,
 * because the backend never delivers that ancestor's directories to a chat
 * running as `principal`. Listing them here would present inert steering as
 * inherited. The person's folders carry no `owner_app` and are in effect for
 * every principal, matching the backend rule exactly (`owner && owner !==
 * principal`). `principal` is the owner of the folder being resolved — the
 * empty string for a person-owned folder.
 */
export function resolveFolderSteeringDirs(
  folders: ChatFolder[],
  folderId: string,
  principal = ''
): string[] {
  // Walk UP collecting each level's dirs, then reverse so the root ancestor
  // comes first — the walk itself is leaf-to-root.
  const levels: string[][] = []
  let current: ChatFolder | undefined = folders.find(f => f.id === folderId)
  const seen = new Set<string>()
  while (current) {
    if (seen.has(current.id)) break // cycle guard
    seen.add(current.id)
    const owner = current.owner_app || ''
    const effective = !owner || owner === principal
    if (effective && Array.isArray(current.steering_dirs) && current.steering_dirs.length) {
      levels.push(current.steering_dirs)
    }
    const parentId = current.parent_id
    current = parentId ? folders.find(f => f.id === parentId) : undefined
  }
  levels.reverse() // root-first
  const out: string[] = []
  const dedup = new Set<string>()
  for (const level of levels) {
    for (const dir of level) {
      if (dedup.has(dir)) continue
      dedup.add(dir)
      out.push(dir)
    }
  }
  return out
}

/**
 * True when a folder-inherited create failed because the folder's project
 * directory no longer exists. Two endpoints say so with HTTP 400: the create
 * itself (`folder_project_invalid`, message `invalid folder project: ...`)
 * and the later slot-project call ("Not a directory"). It is the one create
 * failure the user can fix, so the folder `+`, the phone's new-chat button and
 * the new-chat shortcut name the stale path.
 *
 * createSlot rethrows the ApiError, but createAsyncThunk serializes thrown
 * errors down to {name, message, stack}, so the instance and its `status` are
 * gone by the time `.unwrap()` delivers it. Match the live instance when
 * present and fall back to the serialized shape.
 */
export function isStaleProjectDirError(err: unknown): boolean {
  const isStale = (m: unknown) => m === 'Not a directory'
    || (typeof m === 'string' && m.startsWith('invalid folder project:'))
  if (err instanceof ApiError) return err.status === 400 && isStale(err.message)
  const e = err as { name?: unknown; message?: unknown } | null
  return e?.name === 'ApiError' && isStale(e.message)
}
