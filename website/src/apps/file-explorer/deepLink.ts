/**
 * The Files app's deep link: `/file-explorer?path=<absolute path>`.
 *
 * Another dashboard surface (the chat side panel's file viewer) hands a file
 * to the Files app through this URL. `FileExplorerPage` reads the param once,
 * resolves it through the app's own backend, and opens it in the tab whose root
 * already contains it — or in a new tab rooted at its folder. The page never
 * trusts the raw value: the backend's `resolve` is what says whether the path
 * exists, what it is, and whether this app may show it at all.
 *
 * Pure on purpose. The URL shape, the containment rule and the reveal chain are
 * pinned by `fileExplorerDeepLink.test.ts` rather than by driving the page, and
 * the one consumer outside this app (`MarkdownPanel`'s overflow menu) imports
 * the link builder without pulling the page's component graph behind it.
 */

/** The app's `/api/apps` name, the key the row looks up in the installed list. */
export const FILE_EXPLORER_APP = 'file-explorer'
/** The native route `builtinRegistry` serves the page at. */
export const FILE_EXPLORER_ROUTE = '/file-explorer'
/** Query param naming the path to open. */
export const FILE_EXPLORER_PATH_PARAM = 'path'

/**
 * The URL that opens `path` in the Files app.
 *
 * Callers gate on `isAbsolutePath(path)` first (the page refuses anything else,
 * see `FileExplorerPage`): the Files backend resolves a relative path against
 * the gateway process's working directory, which no link author can know, so a
 * relative value would open whatever happens to sit there.
 */
export function fileExplorerDeepLink(path: string): string {
  return `${FILE_EXPLORER_ROUTE}?${FILE_EXPLORER_PATH_PARAM}=${encodeURIComponent(path)}`
}

/**
 * A path in the form the page compares by. Windows drive (`C:\x`) and UNC
 * (`\\host\share`) paths compare with either separator and case-insensitively;
 * everything else compares byte for byte, since a POSIX path IS case-sensitive.
 */
export const normPath = (x: string) =>
  /^([a-z]:|[\\/]{2})/i.test(x) ? x.replace(/\\/g, '/').toLowerCase() : x

/** `p` is the root `r` itself or a path below it. */
export const underRoot = (p: string, r: string) => {
  const [a, b] = [normPath(p), normPath(r).replace(/\/+$/, '')]
  return a === b || a.startsWith(b + '/') || b === ''
}

/**
 * The directory containing `p`, or `null` at a filesystem root. Separator-aware
 * because the backend prints paths with the host's own separator (`str(Path)`),
 * so on a Windows gateway every one is a backslash; `utils.dirname` is POSIX-only.
 */
export function parentDir(p: string): string | null {
  const trimmed = p.replace(/[\\/]+$/, '')
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  if (cut < 0) return null
  // `/x` → `/`; `C:\x` → `C:\`. Keep the separator so the root stays a path.
  if (cut === 0 || /^[a-z]:$/i.test(trimmed.slice(0, cut))) return trimmed.slice(0, cut + 1)
  const parent = trimmed.slice(0, cut)
  return parent === trimmed ? null : parent
}

/**
 * The directories the tree must have expanded for `dir` to be on screen under
 * `root`: `root` itself, then every directory from just below it down to and
 * including `dir`, outermost first. `[]` when `dir` is not under `root`.
 *
 * Built by cutting `dir` down rather than by joining segments back up, so each
 * entry is byte-identical to the `path` the backend prints on the matching tree
 * node — `FolderTab.expanded` is keyed by that string, and a re-joined path
 * would miss on a Windows separator or a case difference.
 */
export function revealChain(root: string, dir: string): string[] {
  if (!underRoot(dir, root)) return []
  const rootKey = normPath(root).replace(/\/+$/, '')
  const chain: string[] = []
  let cur: string | null = dir
  while (cur !== null && normPath(cur).replace(/\/+$/, '') !== rootKey) {
    chain.push(cur)
    cur = parentDir(cur)
  }
  chain.push(root)
  return chain.reverse()
}
