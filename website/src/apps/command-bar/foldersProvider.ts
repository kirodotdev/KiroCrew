import { createElement } from 'react'
import { Folder } from 'lucide-react'

import { fuzzyMatch, substringIndices } from '../../utils/fuzzyMatch'
import { orderFoldersWithPaths, FOLDER_PATH_SEP, folderNameText } from '../../utils/folderTree'
import { i18nT } from '../../i18n/t'
import type { ChatFolder } from '../../types'
import type { Result, ResourceProvider } from '../../components/commandPalette/types'

/**
 * Folders corpus for the Command Bar's **Search Folders** view.
 *
 * This lives in the app, not in the host's quick-search, and that placement is the
 * feature rather than a filing choice. Reaching a session folder by name is a
 * Command Bar capability: you enter one row and get a list that narrows, the same
 * shape session search and artifact search have here. The host palette
 * deliberately carries no Folders tab, so there is exactly one implementation of
 * "find a folder and land on it" and no second copy to drift from it.
 *
 * Type a folder's name and land ON that folder — the launcher closes, the chat
 * surface takes over, and the sidebar un-hides the folder, expands it and every
 * collapsed ancestor, scrolls it into view and flashes it. That reveal already
 * existed for SESSION rows (`requestSlotReveal`); this provider is what points it
 * at a folder.
 *
 * The corpus is the folder list the sidebar itself renders, read through the
 * SHARED `['chat-folders']` React-Query key, so a search is normally a cache hit
 * with no request at all. Matching is therefore entirely client-side (unlike
 * session search, whose backend does the ranking): {@link fuzzyMatch} supplies both
 * the score and the highlight indices, over two fields —
 *
 *  - the folder's own NAME, which is what the user typed at, and
 *  - its full ancestry PATH, so "kirocrew oss" finds `kirocrew › oss` and a name
 *    that only reads unambiguously in context is still reachable.
 *
 * A path match scores strictly below a name match ({@link PATH_MATCH_PENALTY}) so
 * the folder the user named outranks its own children.
 *
 * Ordering and the breadcrumb both come from `utils/folderTree`, the same module
 * the sidebar's folder pickers use, so the view lists folders in the order the
 * sidebar draws them and spells a path the way the server's `folder_breadcrumb`
 * does. Re-deriving either here would be a second answer to a settled question.
 *
 * `Result` / `ResourceProvider` still come from the host's palette types: that is
 * the row contract the launcher renders and the Enter matrix dispatches, shared by
 * every corpus view in this app. Copying the shape into the app would fork the
 * contract, which is the opposite of what moving this file was for.
 */

const PROVIDER_ID = 'folders'

/**
 * Catalog KEY for the view's label, resolved where the provider object is BUILT
 * (never at module scope, which would freeze the boot language).
 *
 * Reuses `pages.chatSidebar.folders` — the sidebar's own "Folders" heading — rather
 * than adding a second entry: the view and that heading name the same collection of
 * the same objects, so a locale rendering one differently from the other would be
 * inconsistent, not nuanced.
 */
const PROVIDER_LABEL_KEY = 'pages.chatSidebar.folders'

/** Cache the folder list briefly so retyping the same query is free. Matches the
 *  staleTime the sessions provider uses for its own `['chat-folders']` reads, so
 *  the two share cache entries instead of invalidating each other.
 *
 *  Exported because the launcher builds this provider through
 *  {@link createFoldersProvider} with its own React-Query fetcher, and a
 *  hand-copied window there would be a second answer that drifts. */
export const FOLDERS_STALE_MS = 30_000

/**
 * Score subtracted when the query matched only the ancestry PATH and not the
 * folder's own name. Large enough that any name match sorts above any path match
 * (fuzzyMatch scores are bounded far below this), so typing a parent's name lists
 * the parent first and its children under it.
 */
const PATH_MATCH_PENALTY = 1_000_000

/**
 * Injectable dependencies for {@link createFoldersProvider}. Keeping the corpus
 * free of React hooks makes it unit-testable with a plain mock fetch + reveal
 * callback, and leaves the real React-Query + Redux wiring in the one component
 * that owns this app's seams (`CommandBarOverlay`).
 */
export interface FoldersProviderDeps {
  /** Fetch the sidebar folder list (React-Query-cached by the caller). */
  fetchFolders: () => Promise<ChatFolder[]>
  /** Land on a folder: show the chat surface and reveal the folder row. */
  revealFolder: (folderId: string) => void
}

function folderIcon() {
  return createElement(Folder, { className: 'lucide-inline' })
}

/**
 * Build the Folders {@link ResourceProvider} from injected dependencies.
 * Pure (no hooks) so it can be exercised directly in tests.
 */
export function createFoldersProvider(deps: FoldersProviderDeps): ResourceProvider {
  const { fetchFolders, revealFolder } = deps

  return {
    id: PROVIDER_ID,
    // A GETTER, not a plain call: the provider object is built inside a `useMemo`
    // whose deps do not include the language, and `LanguageProvider` re-renders
    // rather than remounting, so `label: i18nT(...)` would keep the pre-switch
    // wording forever. Same reasoning as the sessions provider's label.
    get label() { return i18nT(PROVIDER_LABEL_KEY) },
    icon: folderIcon(),
    // No `minQueryChars`: the corpus is a cached local list, so a single
    // character costs no round trip and narrowing from one letter is useful.
    async search(query: string): Promise<Result[]> {
      const q = query.trim()
      const folders = await fetchFolders()
      // Pre-order (tree) sequence with ancestors + full path already derived, and
      // orphans/cycles already handled. Its order is the ORDER OF THE RESULT LIST
      // for an empty query and the stable tiebreak for a scored one, so the view
      // mirrors the sidebar instead of inventing an alphabetical view of it.
      const ordered = orderFoldersWithPaths(folders)
      // Sidebar tree position per row id, used as the sort tiebreak below. Kept in
      // a parallel Map rather than on the row itself so the shared `Result` type
      // is not widened with a field only this provider can populate.
      const treeOrder = new Map<string, number>()

      const results: Result[] = []
      ordered.forEach(({ folder, ancestors, path }, treeIndex) => {
        const parentPath = ancestors.length ? ancestors.join(FOLDER_PATH_SEP) : undefined
        const nameMatch = fuzzyMatch(q, folderNameText(folder))
        // The ancestry path is matched as one string ("kirocrew › oss › thing"), so
        // a query spanning segments still hits. Only consulted when the name
        // missed: a folder whose name matched is already ranked, and highlighting
        // its path as well would double-mark the same row.
        const pathMatch = nameMatch || !parentPath ? null : fuzzyMatch(q, path)
        if (!nameMatch && !pathMatch) return
        results.push({
          id: `${PROVIDER_ID}:${folder.id}`,
          providerId: PROVIDER_ID,
          title: folderNameText(folder),
          subtitle: parentPath,
          // Highlight the breadcrumb only when the path is WHY the row surfaced.
          // `substringIndices` is computed against the PARENT path actually
          // rendered, not the full path the score came from, so an offset can
          // never point past the end of the string on screen.
          subtitleIndices: pathMatch && parentPath ? substringIndices(q, parentPath) : undefined,
          icon: folderIcon(),
          score: nameMatch ? nameMatch.score : (pathMatch?.score ?? 0) - PATH_MATCH_PENALTY,
          indices: nameMatch ? nameMatch.indices : [],
          // `invoke` rather than `navigate`: landing on a folder is a route change
          // AND a store write (the reveal request the sidebar consumes), and the
          // Enter matrix routes a two-part effect through the action callback.
          // Both Enter and ⌘Enter do the same thing — a folder has no split-pane
          // or new-session variant — so no `onCmdActivate` is bound and the
          // dispatcher's documented fallback to `onActivate` is correct here.
          enter: { kind: 'invoke', run: () => revealFolder(folder.id) },
          onActivate: () => revealFolder(folder.id),
        })
        treeOrder.set(`${PROVIDER_ID}:${folder.id}`, treeIndex)
      })

      // Score first (name matches above path matches by construction), then the
      // SIDEBAR's own order as the tiebreak. Deliberately not `localeCompare`:
      // `folderTree` documents why folder ordering avoids host-locale collation
      // (the same two folders would sort differently for two people), and an
      // alphabetical tiebreak here would reintroduce exactly that.
      results.sort(
        (a, b) => b.score - a.score || (treeOrder.get(a.id) ?? 0) - (treeOrder.get(b.id) ?? 0),
      )
      return results
    },
  }
}
