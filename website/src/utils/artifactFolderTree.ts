import type { ArtifactFolder } from '../types'

/**
 * Pure helpers for the artifact-library folder tree.
 *
 * These operate on the flat `{id, parent_id}` folder list returned by
 * `GET /api/artifact-folders`. All walks are cycle-guarded (visited set /
 * depth cap) because a corrupt `artifact_folders.json` must degrade
 * gracefully, never hang the UI.
 */

const MAX_DEPTH = 20

/** Direct children of `parentId` ("" = root), sorted by order then name.
 * Orphans (parent_id pointing at a missing folder) are treated as roots. */
export function childFolders(folders: readonly ArtifactFolder[], parentId: string): ArtifactFolder[] {
  const ids = new Set(folders.map(f => f.id))
  return folders
    .filter(f => {
      const parent = f.parent_id && ids.has(f.parent_id) ? f.parent_id : ''
      return parent === (parentId || '')
    })
    // Alphabetical, not manual/creation order: folders are persistent
    // containers whose position should be predictable at a glance (artifacts
    // themselves sort by recency, where "latest first" is what's useful).
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) || a.order - b.order)
}

/**
 * True when `candidateId` is `ancestorId` itself or one of its descendants.
 * Used as the client-side cycle guard for drag-to-nest and the move submenu:
 * a folder must never be re-parented into itself or its own subtree.
 */
export function isDescendantFolder(
  folders: readonly ArtifactFolder[],
  ancestorId: string,
  candidateId: string,
): boolean {
  if (!ancestorId || !candidateId) return false
  if (ancestorId === candidateId) return true
  const byId = new Map(folders.map(f => [f.id, f]))
  let cur = byId.get(candidateId)
  const visited = new Set<string>()
  let depth = 0
  while (cur && cur.parent_id && depth < MAX_DEPTH) {
    if (visited.has(cur.id)) return false
    visited.add(cur.id)
    if (cur.parent_id === ancestorId) return true
    cur = byId.get(cur.parent_id)
    depth++
  }
  return false
}

/** All folder ids in the subtree rooted at `folderId` (inclusive), cycle-guarded. */
export function subtreeFolderIds(folders: readonly ArtifactFolder[], folderId: string): string[] {
  const out: string[] = []
  const visited = new Set<string>()
  const walk = (id: string, depth: number) => {
    if (visited.has(id) || depth > MAX_DEPTH) return
    visited.add(id)
    out.push(id)
    for (const child of childFolders(folders, id)) walk(child.id, depth + 1)
  }
  walk(folderId, 0)
  return out
}

/**
 * Impact stats for the delete-folder dialog: total artifacts filed anywhere in
 * the subtree (from each folder's server-computed direct `item_count`) and the
 * number of subfolders below the root (excluding the folder itself).
 */
export function folderSubtreeStats(
  folders: readonly ArtifactFolder[],
  folderId: string,
): { artifactCount: number; subfolderCount: number } {
  const ids = subtreeFolderIds(folders, folderId)
  const byId = new Map(folders.map(f => [f.id, f]))
  let artifactCount = 0
  for (const id of ids) artifactCount += byId.get(id)?.item_count ?? 0
  return { artifactCount, subfolderCount: Math.max(0, ids.length - 1) }
}

/**
 * Breadcrumb chain root→leaf for `folderId` (inclusive). Cycle-guarded;
 * orphaned ancestry simply truncates at the orphan (it renders as a root).
 */
export function folderBreadcrumb(folders: readonly ArtifactFolder[], folderId: string): ArtifactFolder[] {
  const byId = new Map(folders.map(f => [f.id, f]))
  const chain: ArtifactFolder[] = []
  let cur = byId.get(folderId)
  const visited = new Set<string>()
  while (cur && !visited.has(cur.id) && chain.length < MAX_DEPTH) {
    visited.add(cur.id)
    chain.unshift(cur)
    cur = cur.parent_id ? byId.get(cur.parent_id) : undefined
  }
  return chain
}
