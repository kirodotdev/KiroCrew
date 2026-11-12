/**
 * Classify a chat-composer drop BEFORE acting on it, so folders can take the
 * path-insertion route instead of the upload route (issue #743: dropping a
 * folder from the OS file manager tried to upload it).
 *
 * Detection reads `dataTransfer.items` + `webkitGetAsEntry().isDirectory` —
 * the supported directory signal in Chromium (Electron shell AND the
 * browser-served dashboard). `dataTransfer.files` cannot distinguish a folder:
 * it arrives as a plain File with no useful MIME type and a platform-dependent
 * size, so type/size guesses are exactly the bug this replaces.
 *
 * Path resolution: only the desktop shell can see a real filesystem path
 * (pathForFile → webUtils in the preload). In a plain browser a dropped
 * folder's NAME is all we have, and a bare name is NOT a usable path — a
 * misleading relative string inserted silently is worse than today's upload
 * attempt — so a directory without a resolvable path deliberately falls back
 * to the upload route (today's behaviour), unchanged.
 *
 * Mixed drops keep both routes: files upload, folders insert paths.
 */
import { pathForFile } from '../lib/electron'

/**
 * Can a folder at `p` NOT be written as a composer folder token? The token
 * grammar (DIR_TOKEN_RE in fileTokens.ts, shared with the @-picker) cannot
 * carry whitespace or `@` in its body, and parseDirTokens rejects slash-only
 * bodies, so filesystem roots (`/`, `C:\`) and such paths would look like a
 * folder reference but never parse into a chip or serialize on send.
 */
export function isUntokenizableDirPath(p: string): boolean {
  return /[\s@]/.test(p) || /^[/\\]+$/.test(p) || /^[A-Za-z]:[/\\]*$/.test(p)
}

export interface ClassifiedDrop {
  /** Regular files — and directories no real path could be resolved for
   *  (browser fallback) — routed to the existing upload path. */
  files: File[]
  /** Absolute filesystem paths of dropped directories, resolved by the
   *  desktop shell. Routed to composer text insertion. */
  dirPaths: string[]
  /** Resolved paths of dropped directories the composer's folder-token grammar
   *  cannot reference (a space or `@` in the body, or a filesystem root). We
   *  KNOW these are folders and we HAVE their path, so nothing is uploaded and
   *  nothing is inserted — the caller shows the existing "folder can't be
   *  added" notice instead of the generic upload-failed banner (#30416). The
   *  browser no-path case is NOT here: without a path we cannot tell a refused
   *  folder from a plain file, so it stays on the upload route (#743). */
  refusedDirs: string[]
}

export function classifyDrop(dt: DataTransfer): ClassifiedDrop {
  const items = dt.items ? Array.from(dt.items) : []
  // No file-kind items to classify (synthetic events, exotic sources): keep
  // today's behaviour on whatever `files` carries rather than dropping the
  // payload on the floor.
  if (!items.some(it => it.kind === 'file')) {
    return { files: Array.from(dt.files || []), dirPaths: [], refusedDirs: [] }
  }
  const files: File[] = []
  const dirPaths: string[] = []
  const refusedDirs: string[] = []
  for (const item of items) {
    if (item.kind !== 'file') continue
    // Both reads must happen synchronously inside the drop handler — the
    // DataTransferItemList is neutered once the event yields.
    const entry = typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null
    const file = item.getAsFile()
    if (!file) continue
    if (entry?.isDirectory) {
      const p = pathForFile(file)
      // The composer's folder-token grammar (DIR_TOKEN_RE in fileTokens.ts,
      // shared with the @-picker) cannot carry whitespace or `@` in the body,
      // and parseDirTokens rejects slash-only bodies — so filesystem roots
      // (`/`, `C:\`) and such paths would LOOK like a folder reference but
      // never parse into a chip or serialize on send: a silent dead token. We
      // resolved a real path and know it is a folder, so route those to the
      // refused bucket (the caller shows the folder-refused notice) rather than
      // the upload fallback the no-path browser case below still takes.
      if (p && isUntokenizableDirPath(p)) {
        // We resolved a real path and it is a folder, but its path can't be
        // written as a composer folder token (a space or `@`, or a filesystem
        // root). Uploading it would fail with a misleading file-type/size
        // banner, so route it to the refused bucket: nothing is uploaded or
        // inserted, and the caller shows the existing folder-refused notice
        // (#30416).
        refusedDirs.push(p)
        continue
      }
      if (p) {
        dirPaths.push(p)
        continue
      }
      // Browser (no real path visible): a bare name is not a usable path and
      // we cannot tell a refused folder from a plain file, so fall back to the
      // upload route — today's behaviour (#743), unchanged.
      files.push(file)
    } else {
      files.push(file)
    }
  }
  return { files, dirPaths, refusedDirs }
}
