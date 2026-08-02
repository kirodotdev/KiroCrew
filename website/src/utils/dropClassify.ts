/**
 * Classify a drag-and-drop payload into files to upload versus folders to insert
 * as a path into the composer.
 *
 * Why this exists: dragging a folder from Finder into the chat used to upload it
 * as a (garbage, 0-byte) attachment. CLI users expect a folder drop to insert its
 * path as text, the way a terminal does. A `File` for a folder is indistinguishable
 * from a real file by `size`/`type` alone, so the reliable directory signal is
 * `DataTransferItem.webkitGetAsEntry().isDirectory` (present in Chromium browsers
 * and Electron). The absolute path, however, is only obtainable inside Electron
 * (via `webUtils.getPathForFile`), threaded in here as `resolvePath` so this stays
 * a pure, platform-agnostic decision that is regression-tested on its own.
 *
 * Degraded cases, handled explicitly rather than silently uploading garbage:
 *  - directory detected but no path resolvable (plain browser): counted in
 *    `blockedFolders`, never added to `files`.
 *  - the entry API is entirely unavailable (cannot detect a directory at all):
 *    fall back to uploading every `dataTransfer.files` entry (prior behavior).
 */

export interface DropClassification {
  /** Files that should be uploaded (prior behavior for plain file drops). */
  files: File[]
  /** Absolute folder paths to insert into the composer as text. */
  folderPaths: string[]
  /** Folders detected as directories but whose path could not be resolved. */
  blockedFolders: number
}

type PathResolver = (file: File | null) => string | undefined

export function classifyDrop(
  dataTransfer: Pick<DataTransfer, 'items' | 'files'>,
  resolvePath: PathResolver,
): DropClassification {
  const files: File[] = []
  const folderPaths: string[] = []
  let blockedFolders = 0

  const items = dataTransfer.items
  // Only trust the item path when directory detection is actually available;
  // otherwise we cannot tell a folder from a file and must not guess.
  const canDetect =
    !!items &&
    items.length > 0 &&
    Array.from(items).every(
      it => it && it.kind === 'file' && typeof it.webkitGetAsEntry === 'function',
    )

  if (!canDetect) {
    return { files: Array.from(dataTransfer.files ?? []), folderPaths, blockedFolders }
  }

  for (const item of Array.from(items)) {
    const entry = item.webkitGetAsEntry()
    const file = item.getAsFile()
    if (entry?.isDirectory) {
      const path = resolvePath(file)
      if (path) folderPaths.push(path)
      else blockedFolders += 1
    } else if (file) {
      files.push(file)
    }
  }

  return { files, folderPaths, blockedFolders }
}
