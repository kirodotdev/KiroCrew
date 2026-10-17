/**
 * Matches for a filename search over a CUT project tree: the server's hits
 * joined by the rows the tree already lists. Shared by the folder tab
 * (`FolderPanel`) and the Files rail (`FileBrowserRail`), which search the same
 * truncated tree the same way.
 */

/**
 * Directory part of `full` relative to `root`, or '' when the file sits directly
 * in `root`. Separator-agnostic: a Windows gateway returns backslash paths, and a
 * search result and the root it was searched under always agree on which.
 */
function relativeDir(full: string, root: string): string {
  const trimmed = root.replace(/[/\\]+$/, '')
  const rel = full.startsWith(trimmed) ? full.slice(trimmed.length).replace(/^[/\\]+/, '') : full
  const cut = Math.max(rel.lastIndexOf('/'), rel.lastIndexOf('\\'))
  return cut === -1 ? '' : rel.slice(0, cut)
}

/** Merge the server's file matches with the paths a truncated tree already
 *  lists that contain *query*. The server search walks under its own scan
 *  budget, so on a large workspace it can miss a file the tree is showing; the
 *  tree's own paths are the matches the local filter found before the tree was
 *  capped. The server's ranked matches come first, a path both name is listed
 *  once (separators compared in one form, so a Windows backslash result and
 *  the tree's `/` join meet), and the list stops at *limit*, so the "showing
 *  first N" control still asks the server for the next tier. */
export function mergeTreeMatches(
  server: Array<{ path: string; name: string }>,
  serverRoot: string,
  tree: { root: string; paths: string[] } | undefined,
  query: string,
  limit: number,
): Array<{ path: string; name: string; sub: string }> {
  const merged = server.map(m => ({ path: m.path, name: m.name, sub: relativeDir(m.path, serverRoot) }))
  if (!tree) return merged.slice(0, limit)
  const key = (path: string) => path.replace(/\\/g, '/')
  const seen = new Set(merged.map(m => key(m.path)))
  const needle = query.toLowerCase()
  const root = tree.root.replace(/[/\\]+$/, '')
  for (const rel of tree.paths) {
    if (merged.length >= limit) break
    if (!rel.toLowerCase().includes(needle)) continue
    const path = `${root}/${rel}`
    const k = key(path)
    if (seen.has(k)) continue
    seen.add(k)
    // Tree paths are POSIX, so only `/` splits them (`relativeDir` would also
    // split a `\` that a POSIX file name may hold).
    const cut = rel.lastIndexOf('/')
    merged.push({ path, name: rel.slice(cut + 1), sub: cut === -1 ? '' : rel.slice(0, cut) })
  }
  return merged.slice(0, limit)
}
