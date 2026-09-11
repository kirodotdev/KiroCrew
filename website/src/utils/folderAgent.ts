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
