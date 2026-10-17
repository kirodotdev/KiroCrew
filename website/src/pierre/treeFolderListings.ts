/**
 * Folder listings merged into the whole-project tree payload.
 *
 * `GET /api/project/tree` caps folders and files together at 10,000 rows, so on
 * a project larger than that some folders are not rows at all, and the folders
 * the cut reached are named in `truncatedDirectories`. `?dir=<folder>` lists one
 * such folder on its own, under the same cap, with its paths still relative to
 * the project root. Merging that listing into the whole payload is what lets the
 * tree reach a folder past the cap: expanding a cut folder fetches its own
 * listing, and the merged payload is the one the tree renders.
 */

/** The payload fields a merge reads and writes; a subset of `api.projectTree`'s result. */
export interface TreePayload {
  root: string
  paths: string[]
  directories?: string[]
  repo: boolean
  truncated?: boolean
  truncatedDirectories?: string[]
  hiddenOnlyDirectories?: string[]
  unreadableDirectories?: string[]
  linkedDirectories?: string[]
}

/** One folder's listing: the payload plus the folder it lists. */
export interface FolderListing extends TreePayload {
  dir: string
}

/**
 * Most folder listings one tree mount keeps. Each holds up to the whole cap, so
 * an unbounded set would hand the tree an unbounded path list; past this the
 * oldest listing is dropped and its folder shows as cut again.
 */
export const MAX_FOLDER_LISTINGS = 20

const union = (...lists: Array<readonly string[] | undefined>): string[] => {
  const out = new Set<string>()
  for (const list of lists) for (const item of list ?? []) out.add(item)
  return [...out]
}

/**
 * The whole payload with every folder listing folded in.
 *
 * A listed folder, and every folder beneath it, is not cut by the whole
 * listing's cap, so they leave `truncatedDirectories` -- unless the folder's
 * OWN listing is cut there, which names them again. Every other marker is a
 * union: a folder's listing only ever adds what the whole one could not reach.
 * A listing for another root (a stale one across a project switch) is ignored.
 */
export function mergeFolderListings(base: TreePayload, listings: readonly FolderListing[]): TreePayload {
  // A listing without `dir` is a gateway that predates `?dir=` answering with the
  // whole tree again: it adds nothing, so it is not merged.
  const own = listings.filter(listing => listing.root === base.root && typeof listing.dir === 'string' && listing.dir !== '')
  if (own.length === 0) return base
  const listed = new Set(own.map(listing => listing.dir))
  // A folder's own listing covers its whole subtree, so a cut the whole
  // listing named at or below a listed folder is replaced by what that
  // folder's listing says (which names it again if it is still cut).
  // The same holds between listings: a cut a folder's listing names is
  // replaced by a deeper loaded listing that covers it.
  const coveredBy = (dir: string, deeperThan: string | null) => [...listed].some(folder =>
    (deeperThan === null || folder.startsWith(`${deeperThan}/`))
    && (dir === folder || dir.startsWith(`${folder}/`)))
  const truncatedDirectories = union(
    (base.truncatedDirectories ?? []).filter(dir => !coveredBy(dir, null)),
    ...own.map(listing => (listing.truncatedDirectories ?? []).filter(dir => !coveredBy(dir, listing.dir))),
  ).sort()
  return {
    ...base,
    paths: union(base.paths, ...own.map(listing => listing.paths)),
    // A git listing with nothing in it names a folder git ignores (one the
    // filename search found on disk); it is not shown as an empty folder row.
    directories: union(base.directories, ...own.map(listing => (
      listing.repo && listing.paths.length === 0 && (listing.directories ?? []).length === 0
        ? []
        : [listing.dir, ...(listing.directories ?? [])]
    ))),
    truncated: truncatedDirectories.length > 0,
    truncatedDirectories,
    hiddenOnlyDirectories: union(base.hiddenOnlyDirectories, ...own.map(listing => listing.hiddenOnlyDirectories)),
    unreadableDirectories: union(base.unreadableDirectories, ...own.map(listing => listing.unreadableDirectories)),
    linkedDirectories: union(base.linkedDirectories, ...own.map(listing => listing.linkedDirectories)),
  }
}

/**
 * Most rows (paths plus folder rows) the loaded folder listings may hold
 * together: four whole listings' worth. Twenty listings of up to the full cap
 * each would hand the tree some 200,000 rows to reset, dedupe and render.
 */
export const MAX_FOLDER_LISTING_ROWS = 40_000

/**
 * The folders to drop so the listings fit `MAX_FOLDER_LISTING_ROWS`, oldest
 * first. *listings* is in load order (oldest first); the newest always stays,
 * so the folder the user just opened is shown even when it alone is large.
 */
export function foldersOverRowBudget(listings: readonly FolderListing[]): string[] {
  let total = 0
  const keep = new Set<string>()
  for (let i = listings.length - 1; i >= 0; i--) {
    const rows = listings[i].paths.length + (listings[i].directories?.length ?? 0)
    if (keep.size > 0 && total + rows > MAX_FOLDER_LISTING_ROWS) break
    total += rows
    keep.add(listings[i].dir)
  }
  return listings.filter(listing => !keep.has(listing.dir)).map(listing => listing.dir)
}

/** *folders* with *folder* added last, oldest dropped past `MAX_FOLDER_LISTINGS`. */
export function withFolder(folders: readonly string[], folder: string): string[] {
  if (folders.includes(folder)) return [...folders]
  const next = [...folders, folder]
  return next.length > MAX_FOLDER_LISTINGS ? next.slice(next.length - MAX_FOLDER_LISTINGS) : next
}
