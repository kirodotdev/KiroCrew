/** The Older Sessions pane's Kiro CLI tab: the kiro-cli sessions the owner ran
 *  in a terminal, searchable, each opening a read-only preview that can
 *  continue it as a Crew chat. */
import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { keepPreviousData, useInfiniteQuery } from '@tanstack/react-query'
import { Terminal, X } from 'lucide-react'
import { api } from '../../api/client'
import type { KiroCliSessionList, KiroCliSessionRow } from '../../api/client/sessions'
import { SearchInput } from '../../components/ui'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../components/ui/tabs'
import ErrorNotice from '../../components/ErrorNotice'
import KiroCliSessionPreview from '../../components/KiroCliSessionPreview'
import { errMessage } from '../../utils/thunkError'
import { fmtRelativeTime } from '../chat/sessionOrder'
import { dateSegment } from './chrome'
import { readStoredHistorySource, storeHistorySource, type HistorySource } from './persistence'
import { i18nT } from '../../i18n/t'

/** Rows per page: what each "Load more" fetches, from where the loaded rows end. */
const PAGE = 50
/** How long typing settles before the gateway is asked again. */
const SEARCH_DEBOUNCE_MS = 250

// The pane's selected source, shared by the tabs and the footer's Clear button
// (which clears Crew's history and so is only offered on the Crew tab). One
// module-level value rather than sidebar state: the footer and the pane body are
// drawn by a render function that cannot hold state of its own.
// Held here, seeded from storage on first read; writing it back is best-effort,
// so a browser that refuses storage still switches tabs (it just forgets the
// choice on reload).
const sourceListeners = new Set<() => void>()
let currentSource: HistorySource | null = null

function getSource(): HistorySource {
  if (currentSource === null) currentSource = readStoredHistorySource()
  return currentSource
}

function setSource(next: HistorySource): void {
  currentSource = next
  storeHistorySource(next)
  for (const listener of sourceListeners) listener()
}

/** Drop the held choice so the next read comes from storage again (a reload, in tests). */
export function resetHistorySource(): void {
  currentSource = null
}

function subscribeSource(listener: () => void): () => void {
  sourceListeners.add(listener)
  return () => { sourceListeners.delete(listener) }
}

/** The Older Sessions pane's selected history source. */
export function useHistorySource(): HistorySource {
  return useSyncExternalStore(subscribeSource, getSource, getSource)
}

/** Renders *children* only while the pane shows Crew's own history. */
export function CrewHistoryOnly({ children }: { children: ReactNode }) {
  return useHistorySource() === 'crew' ? <>{children}</> : null
}

/**
 * The pane's two histories as tabs: Crew's own sessions (*crew*, the list the
 * pane always showed) and the kiro-cli sessions run in a terminal. Only the
 * selected tab's body is drawn, so the Kiro CLI list is fetched only when it is
 * looked at.
 */
export function HistorySourceTabs({ crew, height, connected, onSelectSlot }: { crew: () => ReactNode; height: number; connected: boolean; onSelectSlot?: (key: string) => void }) {
  const source = useHistorySource()
  return (
    <Tabs value={source} onValueChange={v => setSource(v === 'kiro-cli' ? 'kiro-cli' : 'crew')} layoutId="history-source-tabs">
      <div className="px-2 pb-2">
        <TabsList aria-label={i18nT('pages.chatSidebar.history_source')} className="w-full">
          <TabsTrigger value="crew" className="flex-1 justify-center" data-testid="history-tab-crew">{i18nT('pages.chatSidebar.history_tab_crew')}</TabsTrigger>
          <TabsTrigger value="kiro-cli" className="flex-1 justify-center" data-testid="history-tab-kiro-cli">{i18nT('pages.chatSidebar.history_tab_kiro_cli')}</TabsTrigger>
        </TabsList>
      </div>
      <TabsContent value="crew">{source === 'crew' && crew()}</TabsContent>
      <TabsContent value="kiro-cli"><KiroCliHistory height={height} connected={connected} onSelectSlot={onSelectSlot} /></TabsContent>
    </Tabs>
  )
}

/** The last folder name, the part of a path a row has room for. */
export function folderLabel(cwd: string): string {
  const parts = cwd.split(/[\\/]+/).filter(Boolean)
  return parts.length ? parts[parts.length - 1] : cwd
}

/**
 * Rendered only while the pane's Kiro CLI tab is selected, so the gateway is
 * asked for the list when someone looks at it and not before. The gateway does
 * the filtering (which sessions are a person's, and the search), so this keeps
 * no copy of the rule.
 */
function KiroCliHistory({ height, connected, onSelectSlot }: { height: number; connected: boolean; onSelectSlot?: (key: string) => void }) {
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [previewId, setPreviewId] = useState<string | null>(null)
  useEffect(() => {
    const t = window.setTimeout(() => setSearch(query.trim()), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(t)
  }, [query])
  // One page per request, each starting after the last row loaded: the gateway
  // caps a page's size, so growing one request's limit would stop reaching new
  // rows at that cap. The page resumes from that row, not from a count of rows:
  // a session a terminal touches meanwhile moves to the top and shifts the rest
  // down, which would serve a row already shown again.
  const list = useInfiniteQuery({
    queryKey: ['kiro-cli-sessions', search],
    queryFn: ({ pageParam }) => api.kiroCliSessions(search, PAGE, pageParam),
    initialPageParam: null as KiroCliSessionRow | null,
    getNextPageParam: (last: KiroCliSessionList) =>
      last.has_more && last.sessions.length > 0 ? last.sessions[last.sessions.length - 1] : undefined,
    placeholderData: keepPreviousData,
    // A terminal keeps writing sessions with no event reaching Crew, so the
    // list is read again each time it is opened instead of riding the app's
    // push-driven `staleTime: Infinity`.
    staleTime: 0,
    refetchOnMount: 'always',
    enabled: connected,
  })
  const rows = (list.data?.pages ?? []).flatMap(p => p.sessions)

  const row = (s: KiroCliSessionRow) => (
    <button
      type="button"
      key={s.id}
      disabled={!connected}
      onClick={() => setPreviewId(s.id)}
      title={s.cwd ? `${s.title}\n${s.cwd}` : s.title}
      className="group w-full flex items-start gap-2.5 pr-4 py-2 rounded-md text-left text-sm text-muted transition-all bg-transparent border-none cursor-pointer hover:text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50"
      style={{ paddingLeft: '10px' }}
      data-testid={`kiro-cli-row-${s.id}`}
    >
      <Terminal size={12} className="shrink-0 self-center text-muted" aria-hidden="true" />
      <span className="flex-1 min-w-0 overflow-hidden">
        <span className="text-[11px] font-semibold truncate leading-tight flex items-center gap-1">
          <span className="truncate">{s.cwd ? folderLabel(s.cwd) : i18nT('pages.chatSidebar.kiro_cli_no_folder')}</span>
          <span className="ml-auto text-[11px] font-normal shrink-0">{fmtRelativeTime(s.updated_at)}</span>
        </span>
        <span className="block text-[13px] leading-snug line-clamp-2 break-words">{s.title}</span>
      </span>
    </button>
  )

  let prevSeg = ''
  return (
    <>
      <div className="px-2 pb-1">
        <div className="relative">
          <SearchInput
            className="w-full"
            aria-label={i18nT('pages.chatSidebar.kiro_cli_search')}
            placeholder={i18nT('pages.chatSidebar.kiro_cli_search')}
            value={query}
            onChange={e => setQuery(e.target.value)}
            data-testid="kiro-cli-search"
          />
          {query && (
            <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors" onClick={() => setQuery('')} aria-label={i18nT('pages.chatSidebar.clear_search')}><X size={13} /></button>
          )}
        </div>
      </div>
      <div className="overflow-y-auto scrollbar-none p-2 scroll-shadow" style={{ height: `${height}px`, scrollbarWidth: 'none' }} data-testid="kiro-cli-list">
        <ErrorNotice message={list.error ? errMessage(list.error) : null} askAgent testId="kiro-cli-list-error" />
        {list.isPending && connected && (
          <div className="px-3 py-4 text-[12px] text-muted text-center" role="status">{i18nT('pages.chatSidebar.kiro_cli_loading')}</div>
        )}
        {list.isSuccess && rows.length === 0 && (
          <div className="px-3 py-4 text-[12px] text-muted text-center">
            {search ? i18nT('pages.chatSidebar.no_sessions_match') : i18nT('pages.chatSidebar.kiro_cli_empty')}
          </div>
        )}
        {rows.map((s, idx) => {
          const seg = search ? '' : dateSegment(s.updated_at)
          const showHeader = seg !== '' && seg !== prevSeg
          prevSeg = seg
          return (
            <div key={s.id}>
              {showHeader && (
                <div className="px-2 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
              )}
              {row(s)}
              {idx < rows.length - 1 && !(seg && dateSegment(rows[idx + 1].updated_at) !== seg) && <div className="mx-3 border-b border-border" />}
            </div>
          )
        })}
        {list.hasNextPage && (
          <button
            type="button"
            className="w-full flex justify-center py-2 text-accent text-[13px] font-medium cursor-pointer bg-transparent border-none hover:bg-accent-subtle rounded-md disabled:opacity-50"
            onClick={() => void list.fetchNextPage()}
            disabled={list.isFetchingNextPage}
            data-testid="kiro-cli-load-more"
          >{i18nT('pages.chatSidebar.load_more')}</button>
        )}
      </div>
      <KiroCliSessionPreview sessionId={previewId} onClose={() => setPreviewId(null)} onOpened={onSelectSlot} />
    </>
  )
}
