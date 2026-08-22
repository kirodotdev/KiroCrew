import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Folder, RotateCw, ExternalLink, ChevronUp, Search, X } from 'lucide-react'
import DetailPanel from '../../components/DetailPanel'
import { revealOrOpen, useRevealLabel } from '../../components/FilePathMenu'
import { useBranding } from '../../hooks/useBranding'
import { api } from '../../api/client'
import { fileIcon, colorForExt } from '../../utils/fileIcons'

/** Last path segment, trailing slashes ignored. */
function basename(p: string): string {
  return p.replace(/\/+$/, '').split('/').pop() || p
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

/** Mirrors `max_results` in `dashboard/handlers/files.py`, which truncates BEFORE
 *  responding. Used only to say the list was cut off — if the server's value ever
 *  rises, a full page simply stops carrying the note rather than stating a wrong
 *  total. */
const SEARCH_RESULT_CAP = 15

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
 * Navigation is INTERNAL to the tab: clicking a subdirectory re-targets this
 * panel rather than spawning a tab per directory. `onPathChange` lifts the new
 * path back to the tab record so the strip label follows along. Clicking a file
 * hands off to `onFileOpen`, which opens a normal file tab.
 *
 * Search is RECURSIVE and files-only, which is why it is a second request rather
 * than a filter over `browseFiles`: that endpoint returns ONE directory level, so
 * filtering it client-side could only ever match what is already on screen.
 * `/api/file-search?project=<cwd>&kinds=files` walks the subtree under its own
 * scan budget and re-applies the sensitive-path refusal per hit.
 */
export default function FolderPanel({ path, onClose, onFileOpen, onPathChange }: {
  path: string
  onClose: () => void
  onFileOpen?: (p: string) => void
  onPathChange?: (p: string) => void
}) {
  const { t } = useTranslation()
  const [cwd, setCwd] = useState(path)
  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')

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
  // first keystroke and back on a backspace.
  const searching = debouncedQuery.length >= MIN_QUERY_LEN
  const {
    data: searchData,
    isFetching: isSearching,
    isError: isSearchError,
    error: searchError,
  } = useQuery({
    // react-query hands `queryFn` an AbortSignal and aborts it when the key
    // changes, so a superseded search is cancelled rather than raced.
    queryKey: ['folder-file-search', cwd, debouncedQuery],
    queryFn: ({ signal }) => api.fileSearch(debouncedQuery, cwd, signal, 'files'),
    enabled: searching,
    retry: false,
    staleTime: 5_000,
  })

  const navigate = (next: string) => {
    setCwd(next)
    setQuery('')
    setDebouncedQuery('')
    onPathChange?.(next)
  }

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
            onClick={() => refetch()}
            className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none"
            title={t('pages.chat.folderPanel.refresh')}
            aria-label={t('pages.chat.folderPanel.refresh')}
          >
            <RotateCw size={14} className={isFetching ? 'animate-spin' : undefined} />
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
      <div className="flex-1 overflow-y-auto px-2 py-1.5">
        <div className="text-[10.5px] text-muted/80 font-mono truncate px-2 pb-1.5" title={cwd}>{cwd}</div>
        {searching ? (
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
            {matches.length >= SEARCH_RESULT_CAP && (
              <div className="px-2 py-1.5 text-[10.5px] text-muted/80">
                {t('pages.chat.folderPanel.showing_first_matches', { shown: SEARCH_RESULT_CAP })}
              </div>
            )}
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
