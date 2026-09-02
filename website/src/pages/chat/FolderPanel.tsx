import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Folder, RotateCw, ExternalLink, ChevronDown, ChevronUp, Search, X } from 'lucide-react'
import DetailPanel from '../../components/DetailPanel'
import { revealOrOpen, useRevealLabel } from '../../components/FilePathMenu'
import { useBranding } from '../../hooks/useBranding'
import { useGatewayPlatform } from '../../hooks/useGatewayPlatform'
import { api } from '../../api/client'
import { fileIcon, colorForExt } from '../../utils/fileIcons'
import { PierreWorkspaceTree } from '../../pierre/tree'
import { useTreeState } from './FileBrowserRail'

/** Last path segment, trailing slashes ignored. */
function basename(p: string): string {
  return p.replace(/\/+$/, '').split('/').pop() || p
}

/**
 * Whether two absolute paths name the SAME directory.
 *
 * The rule is deliberately narrow: normalize ONLY what the filesystem itself
 * cannot distinguish. This gate decides which directory's tree the tab renders,
 * so the two error directions are not symmetric -- a false negative merely keeps
 * today's one-level listing, while a false positive renders ANOTHER directory's
 * tree under this tab's name and opens files from it. So:
 *
 * - A trailing slash never distinguishes a directory. Always ignored.
 * - Separator flavour is folded ONLY on a Windows gateway, where `\` and `/` are
 *   the same separator and a backslash cannot appear in a filename at all -- so
 *   folding it there can never alias two real directories. On POSIX a backslash
 *   IS an ordinary filename character, and folding it would make the real
 *   directory `/srv/a\b` compare equal to the different real directory
 *   `/srv/a/b`.
 * - Case is NEVER folded, on either platform. Windows is case-insensitive only
 *   by default: NTFS carries a per-directory case-sensitivity flag (set by WSL
 *   and by `fsutil file setCaseSensitiveInfo`), so two sibling directories
 *   differing only in case can both exist there. Folding case would alias them.
 *   Nothing produces a case-differing spelling of one directory here anyway --
 *   `projectDir` is the slot's server-resolved project and the tab path comes
 *   from a backend-confirmed listing, so both spellings originate from the same
 *   filesystem.
 *
 * The platform is the GATEWAY's, not the browser's: the filesystem that decides
 * what counts as one directory is the one the gateway serves.
 *
 * Deliberately string-only: this gate chooses which component renders, so it
 * must not depend on a request.
 */
function samePath(a: string | null | undefined, b: string | null | undefined, windowsGateway: boolean): boolean {
  if (!a || !b) return false
  const norm = (p: string) => (windowsGateway ? p.replace(/\\/g, '/') : p).replace(/\/+$/, '')
  return norm(a) === norm(b)
}

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

/** The backend ignores a shorter query (`api_file_search` returns an empty result
 *  set under 2 characters), so dispatching one spends a walk that cannot match. */
const MIN_QUERY_LEN = 2

/** Mirrors the DEFAULT `max_results` in `dashboard/handlers/files.py`, which
 *  truncates BEFORE responding. The first page always uses this size; expansion
 *  is an explicit user action on the notice below the list. If the server's
 *  default ever rises, a full page simply stops carrying the note rather than
 *  stating a wrong total. */
const SEARCH_RESULT_CAP = 15

/** Mirrors `_SEARCH_LIMIT_CEILING` in `dashboard/handlers/files.py` — the hard
 *  server-side clamp on the `limit` param. At this tier the notice renders as
 *  plain text again: a button that cannot fetch more would recreate the inert
 *  affordance this control replaces. */
const SEARCH_RESULT_LIMIT_MAX = 60

/** Idle gap before a keystroke becomes a request. The search walks a real
 *  directory tree server-side, so per-keystroke dispatch would queue walks for
 *  prefixes the user has already typed past. */
const SEARCH_DEBOUNCE_MS = 200

/**
 * Directory listing as a side-panel tab body.
 *
 * Exists because a markdown path chip pointing at a directory used to open the
 * file viewer and report "file not found" — the path was real, it just wasn't a
 * file. A directory now gets an affordance that matches what it is.
 *
 * TWO BODIES, one tab. When the tab is rooted at the CURRENT CHAT's project
 * directory it renders the shared `PierreWorkspaceTree` — the same expandable
 * workspace tree the Files rail uses — so descending into a subdirectory expands
 * it in place, several branches can stay open at once, and the tab keeps naming
 * the project. Every other path keeps the one-level listing below, because
 * `/api/project/tree` deliberately answers only for server-known project roots
 * (403 `unknown_project_dir` otherwise) and a directory chip can point anywhere
 * on the gateway's filesystem. `useTreeState` decides availability off the tree
 * component's OWN query key, so the probe costs no extra request, and an error
 * there falls back to the listing rather than showing a dead panel.
 *
 * Navigation in listing mode is INTERNAL to the tab: clicking a subdirectory
 * re-targets this panel rather than spawning a tab per directory. `onPathChange`
 * lifts the new path back to the tab record so the strip label follows along. In
 * tree mode nothing re-targets while browsing descendants, so `onPathChange`
 * fires only for the parent row — which leaves the project root, and therefore
 * leaves tree mode, on purpose.
 *
 * Clicking a file hands off to `onFileOpen` in both modes, which opens a normal
 * file tab.
 *
 * Search is RECURSIVE and files-only in listing mode, which is why it is a second
 * request rather than a filter over `browseFiles`: that endpoint returns ONE
 * directory level, so filtering it client-side could only ever match what is
 * already on screen. `/api/file-search?project=<cwd>&kinds=files` walks the
 * subtree under its own scan budget and re-applies the sensitive-path refusal per
 * hit. In tree mode the same input feeds the tree's own search session instead:
 * the tree already holds the whole path set, so filtering is local and instant
 * and a second endpoint would be redundant.
 */
export default function FolderPanel({ path, projectDir, onClose, onFileOpen, onAddToContext, onPathChange }: {
  path: string
  /** The current chat's project directory, when it has one. Only used to decide
   *  whether this tab is the project root — never as the path to render. */
  projectDir?: string
  onClose: () => void
  onFileOpen?: (p: string) => void
  /** Right-click "Add to context" on a tree row, forwarded to the composer host.
   *  Passed through so the tree here is not a weaker copy of the SAME tree in the
   *  Files rail — a row that offers the action in one surface and not the other is
   *  the divergence a second render site invites. Listing mode has no context
   *  menu, so it is tree-mode only. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  onPathChange?: (p: string) => void
}) {
  const { t } = useTranslation()
  // Still read for `atProjectRoot`'s case-sensitivity flag below; the reveal
  // label it used to spell out now comes from the shared `useRevealLabel`.
  const gatewayPlatform = useGatewayPlatform()
  const [cwd, setCwd] = useState(path)
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [searchLimit, setSearchLimit] = useState(SEARCH_RESULT_CAP)

  // Gate on `cwd`, not on `path`: the header names `cwd`, and after the parent
  // row is used this tab is no longer the project root even though its record
  // still carries the path it was opened on until `onPathChange` lands.
  const atProjectRoot = samePath(cwd, projectDir, gatewayPlatform === 'windows')
  // Passing null keeps the hook call unconditional while spending no request for
  // a tab that could not use the tree anyway.
  const treeState = useTreeState(atProjectRoot ? projectDir : null)
  const treeMode = atProjectRoot && treeState === 'ready'

  // A different query is a different search: expansion applies to the result set
  // the user was looking at, not to whatever they type next. Also covers
  // navigation and re-targeting, both of which clear the query.
  useEffect(() => { setSearchLimit(SEARCH_RESULT_CAP) }, [debouncedQuery, cwd])

  // Re-sync when the tab is re-targeted from outside (a second chip click on a
  // different directory reuses this tab when the id matches). A query typed for
  // the previous directory must not survive: it would render matches from a tree
  // the header no longer names.
  useEffect(() => { setCwd(path); setQuery(''); setDebouncedQuery('') }, [path])

  useEffect(() => {
    const id = setTimeout(() => setDebouncedQuery(query.trim()), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(id)
  }, [query])

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['browse-files', cwd],
    queryFn: () => api.browseFiles(cwd),
    retry: false,
    staleTime: 5_000,
  })

  // Driven by the DEBOUNCED value, so the listing does not blink away on the
  // first keystroke and back on a backspace. Tree mode filters the tree it
  // already holds, so it never spends this request.
  const searching = !treeMode && debouncedQuery.length >= MIN_QUERY_LEN
  const {
    data: searchData,
    isFetching: isSearching,
    isError: isSearchError,
    error: searchError,
  } = useQuery({
    // react-query hands `queryFn` an AbortSignal and aborts it when the key
    // changes, so a superseded search is cancelled rather than raced.
    queryKey: ['folder-file-search', cwd, debouncedQuery, searchLimit],
    queryFn: ({ signal }) => api.fileSearch(debouncedQuery, cwd, signal, 'files', searchLimit),
    enabled: searching,
    retry: false,
    staleTime: 5_000,
    // Keep the current rows on screen while a wider (or new) page is fetched:
    // expansion should widen the list in place, not blank it to a spinner.
    placeholderData: (prev) => prev,
  })

  const navigate = (next: string) => {
    setCwd(next)
    setQuery('')
    setDebouncedQuery('')
    onPathChange?.(next)
  }

  // One refresh button, two things to refresh. In tree mode the listing is not
  // what is on screen, so refetch the queries the tree actually reads — the SAME
  // keys `PierreWorkspaceTreeImpl` uses, which is why this needs no new endpoint
  // and no plumbing into the tree. `refetchQueries` (not `invalidateQueries`) so
  // the spinner tracks a real round trip.
  const qc = useQueryClient()
  const [refreshingTree, setRefreshingTree] = useState(false)
  const refresh = async () => {
    if (!treeMode) { await refetch(); return }
    setRefreshingTree(true)
    try {
      await Promise.all([
        qc.refetchQueries({ queryKey: ['project-tree', projectDir] }),
        qc.refetchQueries({ queryKey: ['git-status', projectDir] }),
      ])
    } finally {
      setRefreshingTree(false)
    }
  }
  const refreshBusy = treeMode ? refreshingTree : isFetching

  const dirs = data?.dirs ?? []
  const files = data?.files ?? []
  const isEmpty = dirs.length === 0 && files.length === 0
  // `parent` comes from the backend (os.path.dirname of the resolved path).
  // Suppress the up-row at the filesystem root, where parent === path.
  const parent = data?.parent && data.parent !== data.path ? data.parent : null

  // Defensive `kind` filter: the server already honours `kinds=files`, but a
  // gateway older than that parameter ignores it and would fold directories into
  // a list whose header promises files.
  const matches = (searchData?.results ?? []).filter(r => r.kind !== 'dir')
  const searchRoot = searchData?.root || cwd
  // While a wider page is in flight, `matches` are placeholder rows from the
  // PREVIOUS tier. `expanding` names that window so the control stays mounted
  // (inert) instead of vanishing mid-fetch, and `shownCount` keeps the notice
  // label describing the rows actually on screen.
  const expanding = isSearching && searchLimit > SEARCH_RESULT_CAP && matches.length < searchLimit
  const shownCount = expanding ? Math.min(matches.length, searchLimit) : searchLimit

  // Name the real application where the gateway HAS one, and fall back to the
  // generic term for Linux and for a platform we could not read. The platform is
  // the GATEWAY's because `/api/reveal` shells out there, and the wording holds for
  // a directory as well as a file — this button reveals `cwd` itself. Shared with
  // every other file-location surface via useRevealLabel.
  const revealLabel = useRevealLabel()
  // `/api/reveal` shells out on the gateway, so revealing `cwd` only makes sense
  // when the browser is on that same machine. A remote/tunneled session would
  // otherwise get a mis-worded "Path copied" alert; hide the button there, the
  // same directLocal gate every other file-location surface applies.
  const { directLocal } = useBranding()

  return (
    <DetailPanel
      embedded
      noPadding
      title={basename(cwd)}
      onClose={onClose}
      customHeader={
        <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
          <Folder size={14} className="shrink-0 text-muted" />
          <span className="text-[12px] text-text-strong truncate" title={cwd}>{basename(cwd)}</span>
          <span className="flex-1" />
          <button
            onClick={refresh}
            className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none"
            title={t('pages.chat.folderPanel.refresh')}
            aria-label={t('pages.chat.folderPanel.refresh')}
          >
            <RotateCw size={14} className={refreshBusy ? 'animate-spin' : undefined} />
          </button>
          {directLocal && (
            <button
              onClick={() => { void revealOrOpen(cwd, 'reveal') }}
              className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none"
              title={revealLabel}
              aria-label={revealLabel}
            >
              <ExternalLink size={14} />
            </button>
          )}
        </div>
      }
    >
      <div className="flex items-center gap-1.5 mx-2 mt-1.5 px-2 h-[28px] shrink-0 rounded-md bg-bg border border-border focus-within:border-accent">
        <Search size={12} className="shrink-0 text-muted" />
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => { if (e.key === 'Escape') { e.preventDefault(); setQuery('') } }}
          placeholder={t('pages.chat.folderPanel.search_files')}
          aria-label={t('pages.chat.folderPanel.search_files')}
          spellCheck={false}
          autoComplete="off"
          className="min-w-0 flex-1 bg-transparent border-none outline-none text-[12px] text-text placeholder:text-muted"
        />
        {treeMode && query && (
          // The same box does two different things in the two bodies: a recursive
          // request that returns a flat "Matches" list, or a filter over the tree
          // already on screen. Listing mode says "includes subfolders" above its
          // results; tree mode has no such header, so the row itself says it --
          // reusing that exact string rather than adding a key that would need
          // thirteen catalogs to explain the same fact.
          <span className="min-w-0 max-w-[45%] truncate text-[10px] text-muted/70">
            {t('pages.chat.folderPanel.includes_subfolders')}
          </span>
        )}
        {query && (
          <button
            onClick={() => setQuery('')}
            className="flex items-center justify-center w-[18px] h-[18px] rounded cursor-pointer text-muted hover:text-text bg-transparent border-none"
            title={t('pages.chat.folderPanel.clear_search')}
            aria-label={t('pages.chat.folderPanel.clear_search')}
          >
            <X size={12} />
          </button>
        )}
      </div>
      <div className={treeMode ? 'flex-1 min-h-0 flex flex-col py-1.5' : 'flex-1 overflow-y-auto px-2 py-1.5'}>
        <div className={`shrink-0 text-[10.5px] text-muted/80 font-mono truncate pb-1.5 ${treeMode ? 'px-3' : 'px-2'}`} title={cwd}>{cwd}</div>
        {treeMode ? (
          <>
            {/* Kept in tree mode so the tab can still step OUT of the project —
                the one navigation the tree cannot express, since the endpoint
                answers for project roots only. Taking it leaves the project
                root, which is what drops this tab back to the listing. */}
            {parent && (
              <div className="shrink-0 px-2 pb-1">
                <Row
                  icon={<ChevronUp size={14} className="shrink-0 text-muted" />}
                  label={t('pages.chat.folderPanel.parent_folder')}
                  title={parent}
                  onActivate={() => navigate(parent)}
                />
              </div>
            )}
            <div className="flex-1 min-h-0 flex flex-col pl-1">
              <PierreWorkspaceTree
                projectDir={projectDir ?? ''}
                onFileOpen={onFileOpen}
                onAddToContext={onAddToContext}
                searchQuery={query || null}
              />
            </div>
          </>
        ) : searching ? (
          <>
            <div className="flex items-center gap-1.5 px-2 pb-1 text-[10px] uppercase tracking-[.06em] text-muted">
              <span>{t('pages.chat.folderPanel.matches')}</span>
              <span className="normal-case tracking-normal text-muted/70">
                {t('pages.chat.folderPanel.includes_subfolders')}
              </span>
            </div>
            {isSearchError && (
              <div className="px-2 py-2 text-[12px] text-danger">
                {(searchError as Error)?.message || t('pages.chat.folderPanel.search_failed')}
              </div>
            )}
            {!isSearchError && isSearching && matches.length === 0 && (
              <div className="px-2 py-2 text-[12px] text-muted">{t('pages.chat.folderPanel.searching')}</div>
            )}
            {!isSearchError && !isSearching && matches.length === 0 && (
              <div className="px-2 py-2 text-[12px] text-muted">{t('pages.chat.folderPanel.no_files_match')}</div>
            )}
            {matches.map(m => {
              const Icon = fileIcon(m.path)
              return (
                <Row
                  key={m.path}
                  icon={<Icon size={14} className={`shrink-0 ${colorForExt(m.path)}`} />}
                  label={m.name}
                  sub={relativeDir(m.path, searchRoot)}
                  title={m.path}
                  onActivate={() => onFileOpen?.(m.path)}
                />
              )
            })}
            {(matches.length >= searchLimit || expanding) && (searchLimit < SEARCH_RESULT_LIMIT_MAX || expanding ? (
              // The notice IS the control: clicking "showing first N" is the
              // natural gesture (#5639), so the text itself requests the next
              // tier. Reusing the notice string keeps the accessible name honest
              // and adds zero i18n keys; the chevron is the visual cue that this
              // is an action, not just a status line.
              //
              // While the wider page loads (`expanding`) the button stays
              // MOUNTED and inert-but-focusable: unmounting it mid-fetch would
              // both show an untruncated-looking list (which this panel defines
              // as "no more matches") and drop keyboard focus to <body> on every
              // activation. `aria-disabled` + the in-handler guard — NOT the
              // `disabled` attribute, which blurs the focused element in real
              // browsers and would drop focus anyway. `shownCount` names the
              // tier the visible rows were fetched for, so the label never
              // overstates what is on screen.
              <button
                type="button"
                aria-disabled={expanding}
                onClick={() => { if (!expanding) setSearchLimit(l => Math.min(l * 2, SEARCH_RESULT_LIMIT_MAX)) }}
                className="flex w-full items-center gap-1 text-left px-2 py-1.5 text-[10.5px] text-muted/80 underline decoration-dotted underline-offset-2 hover:text-text transition-colors aria-disabled:opacity-60 aria-disabled:no-underline"
              >
                {expanding
                  ? <RotateCw size={11} className="shrink-0 animate-spin" />
                  : <ChevronDown size={11} className="shrink-0" />}
                {t('pages.chat.folderPanel.showing_first_matches', { shown: shownCount })}
              </button>
            ) : (
              // At the server ceiling a button could not fetch more; plain text
              // avoids recreating the inert affordance this control replaces.
              <div className="px-2 py-1.5 text-[10.5px] text-muted/80">
                {t('pages.chat.folderPanel.showing_first_matches', { shown: shownCount })}
              </div>
            ))}
          </>
        ) : (
          <>
            {parent && (
              <Row
                icon={<ChevronUp size={14} className="shrink-0 text-muted" />}
                label={t('pages.chat.folderPanel.parent_folder')}
                title={parent}
                onActivate={() => navigate(parent)}
              />
            )}
            {isLoading && <div className="px-2 py-2 text-[12px] text-muted">{t('pages.chat.folderPanel.loading')}</div>}
            {isError && (
              <div className="px-2 py-2 text-[12px] text-danger">
                {(error as Error)?.message || t('pages.chat.folderPanel.unable_to_list_folder')}
              </div>
            )}
            {!isLoading && !isError && isEmpty && (
              <div className="px-2 py-2 text-[12px] text-muted">{t('pages.chat.folderPanel.empty_folder')}</div>
            )}
            {dirs.map(d => (
              <Row
                key={d.path}
                icon={<Folder size={14} className="shrink-0 text-accent" />}
                label={d.name}
                title={d.path}
                onActivate={() => navigate(d.path)}
              />
            ))}
            {files.map(f => {
              const Icon = fileIcon(f.path)
              return (
                <Row
                  key={f.path}
                  icon={<Icon size={14} className={`shrink-0 ${colorForExt(f.path)}`} />}
                  label={f.name}
                  title={f.path}
                  onActivate={() => onFileOpen?.(f.path)}
                />
              )
            })}
          </>
        )}
      </div>
    </DetailPanel>
  )
}

/** One listing row. Mirrors the Files tab's FileRow interaction contract:
 *  clickable, focusable, Enter/Space activates.
 *
 *  `sub` carries a search hit's subfolder. It is right-aligned and truncates from
 *  the START, because the tail of a path is what distinguishes two same-named
 *  files while the head is the part they share. */
function Row({ icon, label, sub, title, onActivate }: {
  icon: React.ReactNode
  label: string
  sub?: string
  title: string
  onActivate: () => void
}) {
  return (
    <div
      className="group flex items-center gap-2 px-2 py-1 rounded-md cursor-pointer hover:bg-bg-hover transition-colors"
      onClick={onActivate}
      title={title}
      role="button"
      tabIndex={0}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onActivate() } }}
    >
      {icon}
      <span className="min-w-0 flex-1 text-[12.5px] text-text truncate">{label}</span>
      {sub && (
        <span
          className="shrink min-w-0 max-w-[45%] text-[10.5px] text-muted font-mono truncate text-right"
          style={{ direction: 'rtl' }}
        >
          {sub}
        </span>
      )}
    </div>
  )
}
