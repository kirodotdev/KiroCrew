import type { ChatFolder } from '../types'

/**
 * Resolve which agent to use when creating a session in a folder.
 * Priority: folder.default_agent → globalDefaultAgent → undefined
 */
export function resolveFolderAgent(
  folders: ChatFolder[],
  folderId: string,
  globalDefaultAgent: string
): string | undefined {
  const folder = folders.find(f => f.id === folderId)
  return folder?.default_agent || globalDefaultAgent || undefined
}

/**
 * Resolve project_dir by walking up the folder hierarchy.
 * Returns the nearest ancestor's project_dir, or undefined.
 */
export function resolveFolderProjectDir(
  folders: ChatFolder[],
  folderId: string
): string | undefined {
  let current: ChatFolder | undefined = folders.find(f => f.id === folderId)
  const seen = new Set<string>()
  while (current) {
    if (seen.has(current.id)) break // cycle guard
    seen.add(current.id)
    if (current.project_dir) return current.project_dir
    current = current.parent_id ? folders.find(f => f.id === current!.parent_id) : undefined
  }
  return undefined
}
