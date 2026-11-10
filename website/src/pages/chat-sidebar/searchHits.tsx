/** What a session row shows while the sidebar search is active: the server's
 *  match-centred snippet in place of the last message, and the folder the row is
 *  filed in. Plus the line above the results (count, clear filters, failure and
 *  scan-window notes). */
import { createContext, useContext, type ReactNode } from 'react'
import { Highlighted } from '../../components/commandPalette/Highlighted'
import ErrorNotice from '../../components/ErrorNotice'
import FolderGlyph from '../../components/FolderGlyph'
import { ROW_STATUS_CLS } from '../../components/listShell'
import { substringIndices } from '../../utils/fuzzyMatch'
import { collectFolderSubtreeIds, folderNameText, orderFoldersWithPaths } from '../../utils/folderTree'
import { highlightText } from '../../utils/highlightText'
import type { ChatFolder } from '../../types'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { cardLineClass, useSessionCardLines } from './cardLines'
import { isPeerRow } from './rowIdentity'
import type { SessionSearchScan } from './search'
import type { Slot } from './types'

/** The active search, as a row reads it. Provided only while the search lane is
 *  drawn; `null` everywhere else, so a row outside a search renders exactly as
 *  before. */
export interface SidebarSearchView {
  query: string
  /** The server's snippet for this row's hit, or undefined (no hit, a title-only
   *  hit, or a peer row, whose key is not a local key). Never built client-side. */
  snippetOf: (slot: Slot) => string | undefined
  /** The row's folder, as its path, or undefined when it is not filed. */
  folderLabelOf: (slot: Slot) => string | undefined
}

export const SidebarSearchContext = createContext<SidebarSearchView | null>(null)

/** Build the view from one search reply's snippets and the folder paths. */
export function makeSidebarSearchView(
  query: string,
  snippets: ReadonlyMap<string, string> | undefined,
  folderOf: Readonly<Record<string, string>>,
  folderPaths: ReadonlyMap<string, string>,
): SidebarSearchView {
  return {
    query: query.trim(),
    snippetOf: slot => (isPeerRow(slot) ? undefined : snippets?.get(slot.key)),
    folderLabelOf: slot => {
      if (isPeerRow(slot)) return undefined
      const id = folderOf[slot.key]
      return id ? folderPaths.get(id) : undefined
    },
  }
}

/**
 * The row's tail line during a search. With no search, or no snippet for this row,
 * it is `tail` unchanged. With a snippet it replaces the last-message `preview`,
 * keeping the unread dot. Any other tail -- an owed approval, a running turn, a crew
 * summary -- wins the line and the snippet is not drawn: a row keeps ONE status
 * line, so its height does not change with its state.
 */
export function SearchHitTail({ slot, tail, preview, dot }: {
  slot: Slot
  tail: ReactNode
  preview: ReactNode
  dot: ReactNode
}) {
  const search = useContext(SidebarSearchContext)
  const lines = useSessionCardLines()
  const snippet = search?.snippetOf(slot)
  if (!search || !snippet || tail !== preview) return <>{tail}</>
  return (
    <div className={`${ROW_STATUS_CLS} text-muted flex items-center gap-1.5 min-w-0${lines.preview > 1 ? ' !items-start' : ''}`}>
      {dot}
      <span className={`${cardLineClass(lines.preview)} min-w-0`} data-session-search-snippet>
        <Highlighted text={snippet} indices={substringIndices(search.query, snippet)} />
      </span>
    </div>
  )
}

/** The folder a search row is filed in, as a small label beside its time. The
 *  query is highlighted in it, which is how a row found by its folder's NAME says so. */
export function SearchFolderLabel({ slot }: { slot: Slot }) {
  const search = useContext(SidebarSearchContext)
  const label = search?.folderLabelOf(slot)
  if (!search || !label) return null
  return (
    <span className="text-muted font-normal truncate max-w-[10rem]" title={label} data-testid="session-search-folder">
      <Highlighted text={label} indices={substringIndices(search.query, label)} />
    </span>
  )
}

/**
 * The folders whose own NAME the query matched, one row each above the session
 * results. The search lane draws no folder blocks, so without these a folder found
 * by name (an empty one especially) would not be on screen at all. The row shows the
 * folder's path with the matched letters marked, and how many of the results sit in
 * its subtree; pressing it leaves the search and reveals the folder in the list.
 */
export function SearchFolderHits({ folders, query, matchIds, isFolderHidden, rows, folderOf, onOpen }: {
  folders: readonly ChatFolder[]
  query: string
  matchIds: ReadonlySet<string> | null
  isFolderHidden: (f: ChatFolder) => boolean
  rows: readonly Slot[]
  folderOf: (slot: Slot) => string | undefined
  onOpen: (folderId: string) => void
}) {
  const q = query.trim().toLowerCase()
  if (!matchIds || !q) return null
  const hits = orderFoldersWithPaths(folders).filter(({ folder }) =>
    matchIds.has(folder.id) && !isFolderHidden(folder) && folderNameText(folder).toLowerCase().includes(q))
  if (hits.length === 0) return null
  return (
    <div className="px-1 pb-1" data-testid="sidebar-search-folder-hits">
      {hits.map(({ folder, path: rawPath }) => {
        // The walk's safety net can hand back a raw stored name; read it as text.
        const path = typeof rawPath === 'string' ? rawPath : folderNameText(folder)
        const subtree = collectFolderSubtreeIds(folders, folder.id)
        const count = rows.filter(slot => { const id = folderOf(slot); return !!id && subtree.has(id) }).length
        return (
          <button
            key={folder.id}
            type="button"
            // Not `data-folder-row`: that marks the tree's folder header, the one
            // place a reveal may scroll to (./reveal.ts), and this row unmounts as
            // the reveal clears the query.
            data-search-folder-hit={folder.id}
            className="w-full flex items-center gap-1.5 px-2 py-1 rounded-md text-[13px] text-text text-left bg-transparent border-none cursor-pointer hover:bg-bg-hover min-w-0"
            title={path}
            onClick={() => onOpen(folder.id)}
          >
            <FolderGlyph color={folder.color} icon={folder.icon} size={14} />
            <span className="flex-1 truncate">{highlightText(path, query.trim(), false, -1)}</span>
            {count > 0 && <span className="text-muted text-[11px] shrink-0">{count}</span>}
          </button>
        )
      })}
    </div>
  )
}

/** The line above the search results: how many rows matched, a way to drop the
 *  filters that may be hiding more (the query stays), and why the list may be short. */
export function SearchResultsHeader({ count, filtersOn, onClearFilters, failed, scan }: {
  count: number
  filtersOn: boolean
  onClearFilters: () => void
  failed: boolean
  scan: SessionSearchScan | null
}) {
  return (
    <div className="px-3 pt-1 pb-2 text-[11px] text-muted select-none" data-testid="sidebar-search-header">
      <div role="status" className="flex items-center gap-1.5 min-w-0">
        <span className="truncate" data-testid="sidebar-search-count">{i18nT('pages.chatSidebar.search_results_count', { count })}</span>
        {filtersOn && (
          <>
            <span aria-hidden>·</span>
            <button
              type="button"
              className="text-accent hover:underline bg-transparent border-none p-0 cursor-pointer shrink-0"
              onClick={onClearFilters}
              data-testid="sidebar-search-clear-filters"
            >
              {i18nT('pages.chatSidebar.search_clear_filters')}
            </button>
          </>
        )}
      </div>
      {/* A read failure with only a search query to lose, so the hand-off is on
          (the Older Sessions pane says the same thing the same way). */}
      {failed && (
        <ErrorNotice
          askAgent
          actionPlacement="below"
          className="mt-1"
          message={i18nT('pages.chatSidebar.history_search_failed')}
          testId="sidebar-search-failed"
        />
      )}
      {scan && (
        <div className="mt-1" data-testid="sidebar-search-scan-note">
          {i18nT(scan.local ? 'pages.chatSidebar.search_scan_cut_short_local' : 'pages.chatSidebar.search_scan_cut_short', {
            searched: fmtNumber(scan.searched),
            total: fmtNumber(scan.total),
          })}
        </div>
      )}
    </div>
  )
}
