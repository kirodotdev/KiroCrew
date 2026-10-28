/** The Older Sessions pane's Kiro CLI tab: the kiro-cli sessions the owner ran
 *  in a terminal, searchable, each opening a read-only preview that can
 *  continue it as a Crew chat. */
import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Terminal, X } from 'lucide-react'
import { api } from '../../api/client'
import type { KiroCliSessionRow } from '../../api/client/sessions'
import { SearchInput } from '../../components/ui'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../components/ui/tabs'
import ErrorNotice from '../../components/ErrorNotice'
import KiroCliSessionPreview from '../../components/KiroCliSessionPreview'
import { errMessage } from '../../utils/thunkError'
import { fmtRelativeTime } from '../chat/sessionOrder'
import { dateSegment } from './chrome'
import { readStoredHistorySource, storeHistorySource, type HistorySource } from './persistence'
import { i18nT } from '../../i18n/t'

/** Rows per page, and the step "Load more" adds. */
const PAGE = 50
/** How long typing settles before the gateway is asked again. */
const SEARCH_DEBOUNCE_MS = 250

// The pane's selected source, shared by the tabs and the footer's Clear button
// (which clears Crew's history and so is only offered on the Crew tab). One
// module-level value rather than sidebar state: the footer and the pane body are
// drawn by a render function that cannot hold state of its own.
// The stored value IS the state (a string, so the snapshot compares by value),
// read on every snapshot so a reload, another tab or a cleared storage is
// followed rather than shadowed by a copy held here.
const sourceListeners = new Set<() => void>()
const getSource = readStoredHistorySource

function setSource(next: HistorySource): void {
  storeHistorySource(next)
  for (const listener of sourceListeners) listener()
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
export function HistorySourceTabs({ crew, height, connected }: { crew: () => ReactNode; height: number; connected: boolean }) {
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
      <TabsContent value="kiro-cli"><KiroCliHistory height={height} connected={connected} /></TabsContent>
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
function KiroCliHistory({ height, connected }: { height: number; connected: boolean }) {
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const [previewId, setPreviewId] = useState<string | null>(null)
  useEffect(() => {
    const t = window.setTimeout(() => { setSearch(query.trim()); setLimit(PAGE) }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(t)
  }, [query])
  const list = useQuery({
    queryKey: ['kiro-cli-sessions', search, limit],
    queryFn: () => api.kiroCliSessions(search, limit, 0),
    placeholderData: keepPreviousData,
    enabled: connected,
  })
  const rows = list.data?.sessions ?? []

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
        {list.data?.has_more && (
          <button
            type="button"
            className="w-full flex justify-center py-2 text-accent text-[13px] font-medium cursor-pointer bg-transparent border-none hover:bg-accent-subtle rounded-md disabled:opacity-50"
            onClick={() => setLimit(l => l + PAGE)}
            disabled={list.isFetching}
            data-testid="kiro-cli-load-more"
          >{i18nT('pages.chatSidebar.load_more')}</button>
        )}
      </div>
      <KiroCliSessionPreview sessionId={previewId} onClose={() => setPreviewId(null)} />
    </>
  )
}
