/**
 * Drag payload for a row dragged out of the dashboard's workspace file tree.
 *
 * A tree row drag is marked with its own MIME type so a drop target can tell
 * it apart from the two drags it already handles: files from the OS (upload)
 * and selected text (a plain-text drop). Browsers expose only `types` while a
 * drag is in flight, so the type alone is the mid-drag signal; the JSON body
 * is readable only at drop time.
 */
import { normalizeWindowsPath } from '../utils/fileTokens'
import { isUntokenizableDirPath } from '../utils/dropClassify'
import { carriesFiles } from './fileDrag'

export const TREE_ENTRY_DRAG_TYPE = 'application/x-kirocrew-tree-entry'

export type TreeEntryDragKind = 'file' | 'dir'

/** One dragged tree row. `path` is ABSOLUTE (a Windows root normalized to
 *  forward slashes, as the row context menu does), with no trailing slash. */
export interface TreeEntryDragPayload {
  path: string
  kind: TreeEntryDragKind
}

export function encodeTreeEntry(entry: TreeEntryDragPayload): string {
  return JSON.stringify({ path: entry.path, kind: entry.kind })
}

/** Parse a drag body; anything that is not a well-formed payload is null. */
export function decodeTreeEntry(raw: string | null | undefined): TreeEntryDragPayload | null {
  if (!raw) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null
  const { path, kind } = parsed as { path?: unknown; kind?: unknown }
  if (typeof path !== 'string' || !path.trim()) return null
  if (kind !== 'file' && kind !== 'dir') return null
  return { path, kind }
}

export function carriesTreeEntry(dataTransfer: DataTransfer | null | undefined): boolean {
  return !!dataTransfer?.types && Array.from(dataTransfer.types).includes(TREE_ENTRY_DRAG_TYPE)
}

export function readTreeEntry(dataTransfer: DataTransfer | null | undefined): TreeEntryDragPayload | null {
  if (!carriesTreeEntry(dataTransfer)) return null
  return decodeTreeEntry(dataTransfer?.getData(TREE_ENTRY_DRAG_TYPE))
}

/**
 * Marks a folder row the composer cannot mention. A folder reference is an
 * `@path/` token whose body holds no whitespace and no `@`, so a folder whose
 * path has either (`My Docs/`, `node_modules/@types/`) has no reference form;
 * the composer refuses it with the no-drop cursor. It is a type of its own
 * because a drop target can read only `types` mid-drag.
 */
export const TREE_ENTRY_REFUSED_TYPE = 'application/x-kirocrew-tree-entry-refused'

/**
 * Mark a drag as a tree row. Widens `effectAllowed` to include `copy`: the
 * tree library starts its own drags as `move`, and a drop target asking for
 * `copy` against a `move`-only drag makes the browser cancel the drop.
 */
export function writeTreeEntry(dataTransfer: DataTransfer, entry: TreeEntryDragPayload, { mentionable = true } = {}): void {
  dataTransfer.setData(TREE_ENTRY_DRAG_TYPE, encodeTreeEntry(entry))
  if (!mentionable) dataTransfer.setData(TREE_ENTRY_REFUSED_TYPE, '1')
  dataTransfer.effectAllowed = 'copyMove'
}

/**
 * The payload for a drag that started on a tree row, found from the event's
 * composed path (the row lives in the tree's shadow root), and whether the
 * composer can mention it. `root` is the project directory the tree paths are
 * relative to. Null when the drag did not start on a row.
 */
export function treeEntryFromComposedPath(
  path: readonly EventTarget[],
  root: string,
): { entry: TreeEntryDragPayload; mentionable: boolean } | null {
  for (const target of path) {
    if (!(target instanceof HTMLElement)) continue
    const rel = target.dataset.itemPath
    if (target.dataset.type !== 'item' || !rel) continue
    const kind: TreeEntryDragKind = target.dataset.itemType === 'folder' ? 'dir' : 'file'
    // Same absolute form the row context menu hands to "Add to chat".
    const base = normalizeWindowsPath(root).replace(/\/+$/, '')
    const tail = rel.replace(/^\/+/, '').replace(/\/+$/, '')
    if (!tail) return null
    return {
      entry: { path: base ? `${base}/${tail}` : tail, kind },
      mentionable: kind === 'file' || !isUntokenizableDirPath(tail),
    }
  }
  return null
}

export type ComposerDropKind = 'tree-entry' | 'tree-refused' | 'files' | 'text' | 'none'

/**
 * Which drop a composer is looking at. The tree check runs first because a
 * tree drag also carries `text/plain` (the row path), which would otherwise
 * read as a text drop.
 */
export function classifyComposerDrop(dataTransfer: DataTransfer | null | undefined): ComposerDropKind {
  if (!dataTransfer) return 'none'
  if (carriesTreeEntry(dataTransfer)) {
    return Array.from(dataTransfer.types).includes(TREE_ENTRY_REFUSED_TYPE) ? 'tree-refused' : 'tree-entry'
  }
  if (carriesFiles(dataTransfer)) return 'files'
  if (Array.from(dataTransfer.types ?? []).includes('text/plain')) return 'text'
  return 'none'
}
