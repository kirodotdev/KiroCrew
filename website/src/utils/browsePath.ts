/**
 * Path-shape helpers for ProjectPicker's Browse tab. They classify the STRING
 * the user typed or the backend returned; nothing here touches the filesystem.
 *
 * `\` is a separator ONLY on a Windows-shaped path (drive-letter `C:...` or
 * UNC `\\...`); on POSIX it is a legal filename character, so every helper
 * keys on the shape first.
 */

/** A drive-letter (`C:\Users`) or UNC (`\\server\share`) path. */
export function isWindowsPath(path: string): boolean {
  return /^[A-Za-z]:/.test(path) || path.startsWith('\\\\')
}

/** Bare drive root: `C:`, `C:\` or `C:/`. Stripping its separator would yield a drive-RELATIVE path. */
function isWindowsDriveRoot(path: string): boolean {
  return /^[A-Za-z]:[\\/]?$/.test(path)
}

/** The separator to append after `path` so the user can keep typing the next segment. */
export function pathSeparator(path: string): string {
  return isWindowsPath(path) ? '\\' : '/'
}

/** Does `path` end in a separator that is a separator FOR ITS SHAPE? */
export function endsWithSeparator(path: string): boolean {
  return isWindowsPath(path) ? /[\\/]$/.test(path) : path.endsWith('/')
}

/**
 * Strip trailing separators for committing/browsing, keeping bare roots intact:
 * POSIX `/`, and a Windows drive root `C:\` / `C:/`. A Windows result is never
 * the bare `C:` — that is a drive-RELATIVE path the backend would resolve to
 * that drive's current directory — so `C:\\` (doubled, as a stray keystroke
 * types it) collapses to `C:\`, not to `C:`.
 */
export function stripTrailingSeparator(path: string): string {
  if (isWindowsPath(path)) {
    if (isWindowsDriveRoot(path)) return path
    const stripped = path.replace(/[\\/]+$/, '')
    return /^[A-Za-z]:$/.test(stripped) ? stripped + (path.includes('/') && !path.includes('\\') ? '/' : '\\') : stripped
  }
  return path.replace(/\/+$/, '') || '/'
}

/**
 * The last path segment, for filtering the listed children by what was typed.
 * `\` splits only a Windows-shaped path; on POSIX it is part of the name, so
 * `/srv/odd\name` filters by `odd\name`, not by `name`.
 */
export function lastSegment(path: string): string {
  return path.split(isWindowsPath(path) ? /[\\/]/ : '/').pop() || ''
}

/**
 * The browse endpoint answers `parent: ""` for a Windows drive root: the level
 * above it is the virtual drive list (`?drives=1`), not a directory. A POSIX `/`
 * answers `parent: "/"` (equal to itself), which reads as "top".
 */
export function parentIsDriveList(path: string, parent: string): boolean {
  return path !== '' && parent === ''
}
