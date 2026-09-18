import type { ChatFolder } from '../types'

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
 */
export function resolveFolderSteeringDirs(
  folders: ChatFolder[],
  folderId: string
): string[] {
  // Walk UP collecting each level's dirs, then reverse so the root ancestor
  // comes first — the walk itself is leaf-to-root.
  const levels: string[][] = []
  let current: ChatFolder | undefined = folders.find(f => f.id === folderId)
  const seen = new Set<string>()
  while (current) {
    if (seen.has(current.id)) break // cycle guard
    seen.add(current.id)
    if (Array.isArray(current.steering_dirs) && current.steering_dirs.length) {
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
