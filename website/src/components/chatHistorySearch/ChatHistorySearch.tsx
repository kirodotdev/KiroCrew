/** One chat's history search, drawn as a Search tab of the chat's side panel.
 *
 *  Shared by the session chat and a bot chat. It searches the
 *  chat's whole transcript on the server (`GET /api/chat/slots/{slot}/history/
 *  search`, newest first, in bounded steps), never only the loaded rows. With
 *  nothing typed it offers quick entries (a day and each message type); with a
 *  query or an entry it lists hits under one tab per type (`TABS`). A
 *  hit opens a read-only preview with the messages around it in the same panel
 *  (`ChatHistoryPreview`). The chat itself never loads or scrolls to a hit,
 *  which can sit tens of thousands of rows back. */
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { AppWindow, CalendarDays, File, FileCode, FileText, Image as ImageIcon, Link2, Pin, Search, Video, X } from 'lucide-react'

import { api } from '../../api/client'
import type { ChatHistoryHit, ChatHistoryKind, ChatHistorySearchParams } from '../../api/client'
import { SEARCH_MIN_CHARS } from '../../api/client/sessions'
import { useDebouncedValue } from '../../hooks/useDebouncedValue'
import { useImeGuard } from '../../hooks/useImeGuard'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { fmtDate, fmtDateNumeric, fmtNumber, fmtTime, fmtWeekday } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { pinnedImageUrl } from '../../utils/pinnedPrompt'
import { matchRanges } from '../globalChatSearch/highlight'
import CrewAvatar from '../CrewAvatar'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import ChatHistoryPreview, { type PreviewActions } from './ChatHistoryPreview'
import { MessageSenderName } from '../../pages/chat/shared-crew/MessageByline'
import MonthCalendar, { dayDate } from './MonthCalendar'

const DEBOUNCE_MS = 250
/** Steps searched on their own while the list is still short; past it, a button. */
const AUTO_STEPS = 8
const AUTO_HITS = 20
/** Re-reads of the counts while the chat's search cache is still being built:
 *  1 s, 2 s, then 4 s after each reply that says `indexing`. */
const INDEXING_RETRIES = 3
const INDEXING_FIRST_RETRY_MS = 1000
const DAY_S = 86_400
const DAY_RE = /^(\d{4})-(\d{2})-(\d{2})$/

/** Epoch seconds of local midnight starting *day* (`YYYY-MM-DD`), plus *addDays*. */
export function dayEpoch(day: string, addDays = 0): number | undefined {
  const m = DAY_RE.exec(day)
  if (!m) return undefined
  return Math.floor(new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + addDays).getTime() / 1000)
}

/** A hit's time as a chat list shows it: the time today, the weekday this
 *  week, the date before that. */
export function hitTimeLabel(ts: string | undefined, now: Date = new Date()): string {
  if (!ts) return ''
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ''
  const dayStart = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const daysAgo = Math.round((dayStart(now) - dayStart(d)) / (DAY_S * 1000))
  if (daysAgo <= 0) return fmtTime(d)
  if (daysAgo < 7) return fmtWeekday(((d.getDay() + 6) % 7) + 1, 'long')
  return fmtDateNumeric(d)
}

/** *text* with *query*'s words drawn in the accent colour. */
function marked(text: string, query: string): ReactNode[] {
  const out: ReactNode[] = []
  let pos = 0
  for (const [start, end] of matchRanges(text, query)) {
    if (start > pos) out.push(text.slice(pos, start))
    out.push(<span key={start} className="text-accent font-medium">{text.slice(start, end)}</span>)
    pos = end
  }
  if (pos < text.length) out.push(text.slice(pos))
  return out
}

/** Each tab's `kind`, label key and icon (the quick entries and the result
 *  rows draw the same icon for that type). */
const TABS: ReadonlyArray<[ChatHistoryKind, string, ReactNode]> = [
  ['', 'components.chatHistorySearch.tab_all', null],
  ['images', 'components.chatHistorySearch.tab_images', <ImageIcon size={15} />],
  ['files', 'components.chatHistorySearch.tab_files', <File size={15} />],
  ['links', 'components.chatHistorySearch.tab_links', <Link2 size={15} />],
  ['videos', 'components.chatHistorySearch.tab_videos', <Video size={15} />],
  ['pdfs', 'components.chatHistorySearch.tab_pdfs', <FileText size={15} />],
  ['html', 'components.chatHistorySearch.tab_html', <FileCode size={15} />],
  ['widgets', 'components.chatHistorySearch.tab_widgets', <AppWindow size={15} />],
  ['pinned', 'components.chatHistorySearch.tab_pinned', <Pin size={15} />],
]
/** A bot chat has no pins (only the session chat pins a message), so it gets no Pinned. */
const BOT_TABS = TABS.filter(([value]) => value !== 'pinned')

export interface ChatHistorySearchProps extends PreviewActions {
  slotKey: string
  /** In a bot chat: the bot, whose name and avatar mark its own messages.
   *  Absent in a session chat, whose rows carry no avatar. */
  bot?: { name: string; seed: string; avatar?: unknown }
  /** Close the panel (Cancel, Escape). */
  onClose: () => void
  /** Bumped each time the find shortcut fires, to focus the field again. */
  focusNonce?: number
  /** Mark the panel as the Escape owner. Off when the host element carries the
   *  mark instead, only while it is on screen (`historySearchHostMarks`). */
  escapeOwner?: boolean
}

export default function ChatHistorySearch({ slotKey, bot, onClose, focusNonce, escapeOwner = true, onQuoteMessage, onForkMessage }: ChatHistorySearchProps) {
  const ime = useImeGuard()
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const [q, setQ] = useState('')
  const [day, setDay] = useState('')
  const [kind, setKind] = useState<ChatHistoryKind>('')
  const [picking, setPicking] = useState(false)
  const [active, setActive] = useState(0)
  const [selected, setSelected] = useState<ChatHistoryHit | null>(null)

  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.focus()
    el.select()
  }, [focusNonce])

  const query = useDebouncedValue(q.trim(), DEBOUNCE_MS)
  const textOn = query.length >= SEARCH_MIN_CHARS
  const params: ChatHistorySearchParams = {
    q: textOn ? query : undefined,
    kind: kind || undefined,
    since: day ? dayEpoch(day) : undefined,
    until: day ? dayEpoch(day, 1) : undefined,
  }
  const enabled = textOn || !!kind || !!day
  const search = useInfiniteQuery({
    queryKey: ['chat-history-search', slotKey, params],
    queryFn: ({ pageParam, signal }) => api.chatHistorySearch(slotKey, { ...params, before: pageParam }, signal),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: last => last.next_before ?? undefined,
    enabled,
    staleTime: 15_000,
  })
  const hits = useMemo(() => (enabled ? (search.data?.pages ?? []).flatMap(p => p.hits) : []), [enabled, search.data])
  // Every tab's count comes from one whole-chat read keyed by the query and
  // day alone, never the tab: a tab switch reuses it, so no count moves. A
  // reply for an older query lands under its own key and is never shown.
  const countParams = { q: params.q, since: params.since, until: params.until }
  const countsRead = useQuery({
    queryKey: ['chat-history-counts', slotKey, countParams],
    queryFn: ({ signal }) => api.chatHistoryCounts(slotKey, countParams, signal),
    enabled,
    staleTime: 60_000,
    // While the server builds the chat's search cache it answers from a row
    // walk; read again a few times so the whole-chat counts show without a
    // new search. `dataUpdateCount` is the replies this key has had.
    refetchInterval: ({ state }) => (state.data?.indexing && state.dataUpdateCount <= INDEXING_RETRIES
      ? INDEXING_FIRST_RETRY_MS * 2 ** (state.dataUpdateCount - 1)
      : false),
  })
  // A walk the server cut at its row budget is not the whole chat, so its
  // numbers (a 0 above all) are not shown as totals.
  const counts: Partial<Record<ChatHistoryKind, number>> | undefined = enabled && countsRead.data?.complete ? countsRead.data.counts : undefined
  const pageCount = search.data?.pages.length ?? 0
  // A step can scan a stretch with no hit at all, so a short list keeps
  // searching older rows on its own, a few steps at most.
  useEffect(() => {
    if (enabled && search.hasNextPage && !search.isFetching && !search.isFetchNextPageError && hits.length < AUTO_HITS && pageCount < AUTO_STEPS) {
      void search.fetchNextPage()
    }
  }, [enabled, search, hits.length, pageCount])
  useEffect(() => { setActive(0) }, [query, kind, day])
  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' })
  }, [active])
  // The tab row scrolls sideways on a narrow panel: keep the chosen tab in view.
  // The counts arrive after the tab is chosen and widen the tabs before it, and
  // a chosen day widens the Date chip, which narrows the row; either can push
  // the chosen tab back out, so both scroll it in again.
  const tabsRef = useRef<HTMLDivElement | null>(null)
  const [attachTabEdges, tabEdges, remeasureTabEdges] = useScrollEdges<HTMLDivElement>()
  const setTabsScroller = useCallback((node: HTMLDivElement | null) => { tabsRef.current = node; attachTabEdges(node) }, [attachTabEdges])
  useEffect(() => {
    // Only the tab scroller moves: `scrollIntoView` would also scroll its
    // ancestors and push the pinned All tab out of view.
    const scroller = tabsRef.current
    const tab = scroller?.querySelector<HTMLElement>('[aria-selected="true"]')
    if (scroller && tab) {
      const box = scroller.getBoundingClientRect()
      const at = tab.getBoundingClientRect()
      const left = at.left - box.left + scroller.scrollLeft
      const right = left + at.width
      if (left < scroller.scrollLeft) scroller.scrollLeft = left
      else if (right > scroller.scrollLeft + scroller.clientWidth) scroller.scrollLeft = right - scroller.clientWidth
    }
    remeasureTabEdges()
  }, [kind, enabled, counts, day, remeasureTabEdges])
  // On a phone the search fills the screen in the side panel's overlay, over
  // the composer the quote went into, so quoting ends the search, the host
  // closes the overlay, and the composer shows the quote.
  const isMobile = useIsMobile()
  const quote = useMemo(() => (onQuoteMessage
    ? (...args: Parameters<typeof onQuoteMessage>) => {
      onQuoteMessage(...args)
      if (isMobile) onClose()
    }
    : undefined), [onQuoteMessage, isMobile, onClose])

  const reset = () => { setQ(''); setDay(''); setKind(''); setPicking(false); setSelected(null) }
  const openKind = (k: ChatHistoryKind) => { setKind(k); setPicking(false); setSelected(null) }
  const pickDay = (d: string) => { setDay(d); setPicking(false); setSelected(null) }
  const clearOrPickDay = () => {
    if (!day) { setPicking(true); return }
    setDay('')
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') { e.preventDefault(); onClose(); return }
    if (e.key === 'Enter') { if (ime.claimEnter(e) && hits[active]) setSelected(hits[active]); return }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    if (hits.length === 0) return
    if (!ime.claimKey(e)) return
    e.preventDefault()
    const down = e.key === 'ArrowDown'
    setActive(i => (down ? Math.min(i + 1, hits.length - 1) : Math.max(i - 1, 0)))
  }

  const speaker = (hit: ChatHistoryHit) => (hit.role === 'user'
    ? i18nT('components.chatHistorySearch.role_you')
    // The bot's own name in its chat, Agent in a session chat.
    : bot?.name || i18nT('components.chatHistorySearch.role_agent'))

  const tabs = bot ? BOT_TABS : TABS
  const tabButton = ([value, key]: (typeof TABS)[number]) => {
    // Once the counts arrive every tab shows its number, 0 included.
    const count = counts ? counts[value] ?? 0 : undefined
    return (
      <button
        key={value || 'all'}
        type="button"
        role="tab"
        aria-selected={kind === value}
        onClick={() => { setKind(value); setSelected(null) }}
        className={`shrink-0 whitespace-nowrap px-3 py-2 text-[13px] cursor-pointer border-b-2 -mb-px focus-ring ${kind === value ? 'border-accent text-text font-medium' : 'border-transparent text-muted hover:text-text'}`}
      >
        {i18nT(key)}
        {count !== undefined && <span className="text-[12px] text-muted tabular-nums" data-testid={`chat-history-tab-count-${value || 'all'}`}>{' '}{fmtNumber(count)}</span>}
      </button>
    )
  }
  let status: string | null = null
  if (enabled && !search.isError) {
    if (search.isFetching && hits.length === 0) status = i18nT('components.chatHistorySearch.searching')
    else if (search.data && hits.length === 0 && !search.hasNextPage) status = i18nT(kind === 'pinned' ? 'components.chatHistorySearch.no_pinned' : 'components.chatHistorySearch.no_results')
    else if (search.data && hits.length === 0) status = i18nT('components.chatHistorySearch.no_recent_results')
  }

  return (
    // The panel answers Escape itself, so the composer's Escape-to-stop yields
    // to it (chat-input/escapeOwners.ts).
    <div {...(escapeOwner ? { 'data-escape-owner': '' } : {})} className="flex flex-col h-full min-h-0 bg-bg" data-testid="chat-history-search">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <div className="flex flex-1 min-w-0 items-center gap-2 rounded-lg bg-bg-hover px-2.5 py-1.5 focus-within:ring-1 focus-within:ring-accent">
          <Search size={15} className="shrink-0 text-muted" aria-hidden />
          <input
            ref={inputRef}
            data-history-search-field=""
            value={q}
            onChange={e => { setQ(e.target.value); setSelected(null) }}
            onKeyDown={onKeyDown}
            {...ime.bindComposition()}
            placeholder={i18nT('components.chatHistorySearch.placeholder')}
            aria-label={i18nT('components.chatHistorySearch.title')}
            aria-controls="chat-history-search-results"
            /* focus-cue-ok: the cue is the wrapper's focus-within ring above. */
            className="flex-1 min-w-0 bg-transparent border-none outline-hidden text-sm text-text placeholder:text-muted"
          />
          {(q || kind || day) && (
            <button type="button" onClick={() => { reset(); inputRef.current?.focus() }} className="shrink-0 p-0.5 rounded-full text-muted hover:text-text cursor-pointer focus-ring" aria-label={i18nT('components.chatHistorySearch.clear')}>
              <X size={14} />
            </button>
          )}
        </div>
        <button type="button" onClick={onClose} className="shrink-0 px-1 text-sm text-accent hover:underline cursor-pointer focus-ring rounded">
          {i18nT('components.chatHistorySearch.cancel')}
        </button>
      </div>

      {selected && (
        <ChatHistoryPreview
          key={`${selected.index}:${selected.mid ?? ''}`}
          slotKey={slotKey}
          hit={selected}
          query={textOn ? query : ''}
          onBack={() => setSelected(null)}
          onHandoff={onClose}
          onQuoteMessage={quote}
          onForkMessage={onForkMessage}
        />
      )}

      <div className={`${selected ? 'hidden' : 'flex'} flex-1 min-h-0 flex-col`}>
        {picking ? (
          <MonthCalendar value={day} onPick={pickDay} />
        ) : !enabled ? (
          // Every type has its entry: two columns fit a phone without scrolling.
          <div className="grid grid-cols-2 gap-y-5 px-6 pt-10 text-sm" data-testid="chat-history-entries">
            <Entry icon={<CalendarDays size={15} />} label={i18nT('components.chatHistorySearch.entry_date')} onClick={() => setPicking(true)} />
            {tabs.filter(([value]) => value).map(([value, key, icon]) => (
              <Entry key={value} icon={icon} label={i18nT(key)} onClick={() => openKind(value)} />
            ))}
          </div>
        ) : (
          <>
            <div className="flex items-end gap-x-1 px-2 border-b border-border">
              {/* All stays pinned at the start, so the way back to every
                  result never scrolls away. The other tabs share one row that
                  scrolls sideways when the panel is narrow (a phone), its
                  scrollbar hidden, its clipped edges faded (`edge-fade-x`), and
                  the chosen tab kept in view. The row scrolls sideways only:
                  `overflow-x-auto` alone turns overflow-y to auto too, and the
                  tabs' 1px underline overlap (-mb-px) then lets a phone drag
                  their labels up and down over the list below. */}
              <div role="tablist" aria-label={i18nT('components.chatHistorySearch.tabs_label')} className="flex items-end min-w-0 flex-1" data-testid="chat-history-tablist">
                {tabButton(tabs[0])}
                <div
                  ref={setTabsScroller}
                  data-fade-left={tabEdges.left ? '' : undefined}
                  data-fade-right={tabEdges.right ? '' : undefined}
                  className="edge-fade-x flex flex-nowrap min-w-0 flex-1 overflow-x-auto overflow-y-hidden overscroll-x-contain touch-pan-x scrollbar-none"
                  data-testid="chat-history-tabs"
                >
                  {tabs.slice(1).map(tabButton)}
                </div>
              </div>
              <button
                type="button"
                onClick={clearOrPickDay}
                className="shrink-0 mb-1.5 inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[12px] text-muted hover:text-text cursor-pointer focus-ring"
                aria-label={day ? i18nT('components.chatHistorySearch.every_day') : i18nT('components.chatHistorySearch.entry_date')}
              >
                <CalendarDays size={12} aria-hidden />
                {day ? <>{fmtDate(dayDate(day))}<X size={12} aria-hidden /></> : i18nT('components.chatHistorySearch.entry_date')}
              </button>
            </div>
            <div
              id="chat-history-search-results"
              ref={listRef}
              role="listbox"
              aria-label={i18nT('components.chatHistorySearch.results_label')}
              className="flex-1 min-h-0 overflow-y-auto"
            >
              {enabled && countsRead.isError && (
                <div className="px-3 pt-2">
                  {/* Only the tab numbers are missing: the list below still works. */}
                  <ErrorNotice variant="inline" impact="minor" message={i18nT('components.chatHistorySearch.counts_error')} askAgent onHandoff={onClose} testId="chat-history-counts-error" />
                </div>
              )}
              {status && <p role="status" className="px-4 py-6 text-center text-[13px] text-muted">{status}</p>}
              {enabled && search.isError && (
                <div className="px-3 py-2">
                  {/* The hand-off loses nothing: the panel holds only a search. */}
                  <ErrorNotice variant="inline" message={i18nT('components.chatHistorySearch.error')} askAgent onHandoff={onClose} testId="chat-history-search-error" />
                </div>
              )}
              {hits.map((hit, i) => (
                <button
                  type="button"
                  key={`${hit.index}:${hit.mid ?? ''}`}
                  role="option"
                  aria-selected={i === active}
                  data-active={i === active}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => { setActive(i); setSelected(hit) }}
                  className={`w-full flex items-start text-left px-3 py-2.5 border-b border-border/60 cursor-pointer ${i === active ? 'bg-bg-hover' : 'hover:bg-bg-hover'}`}
                >
                  <span className="flex flex-1 min-w-0 flex-col gap-0.5">
                    <span className="flex items-center gap-2">
                      {/* A bot's messages carry its small avatar before its name, on
                          the sender line, so every row's text starts at one edge. */}
                      <span className="flex flex-1 min-w-0 items-center gap-1.5 text-[12px] text-muted">
                        {bot && hit.role === 'assistant' && <span className="shrink-0 inline-flex" data-testid="chat-history-bot-avatar" aria-hidden><CrewAvatar seed={bot.seed} avatar={bot.avatar} size={16} /></span>}
                        <span className="min-w-0 truncate">{hit.role === 'user'
                          ? <MessageSenderName meta={hit.meta} slotKey={slotKey} fallback={speaker(hit)} />
                          : speaker(hit)}</span>
                      </span>
                      {hit.ts && <time dateTime={hit.ts} className="shrink-0 text-[12px] text-muted tabular-nums">{hitTimeLabel(hit.ts)}</time>}
                    </span>
                    {hit.snippet && hit.role !== 'file' && <span className="min-w-0 break-words line-clamp-2 text-[13px] text-text">{textOn ? marked(hit.snippet, query) : hit.snippet}</span>}
                    <HitBadges hit={hit} />
                  </span>
                </button>
              ))}
              {/* Offered with no hit too: the steps run on their own stop
                  short of the first message, and older rows may still match. */}
              {enabled && search.hasNextPage && !(search.isFetching && hits.length === 0) && (search.isFetchNextPageError || pageCount >= AUTO_STEPS || hits.length >= AUTO_HITS) && (
                <div className="flex justify-center py-2">
                  <Btn busy={search.isFetchingNextPage} onClick={() => void search.fetchNextPage()}>
                    {i18nT('components.chatHistorySearch.search_older')}
                  </Btn>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function Entry({ icon, label, onClick }: { icon: ReactNode; label: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="inline-flex items-center justify-center gap-1.5 py-1 text-accent hover:underline cursor-pointer focus-ring rounded">
      <span aria-hidden>{icon}</span>
      {label}
    </button>
  )
}

/** An absolute local path, the only thumbnail the panel loads: a URL would
 *  fetch a remote image with no consent. The server sends only these too. */
const LOCAL_PATH_RE = /^(?:\/(?!\/)|[A-Za-z]:[\\/]|\\\\[^\\/])/

/** A named attachment on a result row: its type's icon and its name. A video
 *  is an icon, never a player that would load or play it. */
function NamedBadge({ icon, name, kind }: { icon: ReactNode; name: string; kind: string }) {
  return (
    <span className="inline-flex min-w-0 max-w-full items-center gap-1 rounded border border-border px-1.5 py-0.5" data-testid={`chat-history-${kind}-badge`}>
      <span className="shrink-0 inline-flex" aria-hidden>{icon}</span>
      <span className="min-w-0 truncate">{name}</span>
    </span>
  )
}

/** The icon of a sent file's card, by its type. */
function sentIcon(hit: ChatHistoryHit): ReactNode {
  if (hit.video_count) return <Video size={12} />
  if (hit.pdf_count) return <FileText size={12} />
  if (hit.html_count) return <FileCode size={12} />
  if (hit.image_count) return <ImageIcon size={12} />
  return <File size={12} />
}

function HitBadges({ hit }: { hit: ChatHistoryHit }) {
  const images = (hit.images ?? []).filter(src => LOCAL_PATH_RE.test(src))
  // A sent file's card is one file: its icon and name, which is also its
  // excerpt, so the row draws it here in place of the excerpt line.
  if (hit.role === 'file') {
    return (
      <span className="flex flex-wrap items-center gap-1.5 pt-1 text-[12px] text-muted">
        {images.map(src => (
          <img key={src} src={pinnedImageUrl(src)} alt="" loading="lazy" className="h-12 w-12 rounded object-cover border border-border" />
        ))}
        <NamedBadge kind="sent" icon={sentIcon(hit)} name={hit.snippet} />
      </span>
    )
  }
  const files = hit.file_count ?? 0
  // shortcut: names past the server's first few (`MAX_NAMES`) are not listed;
  // add a "+N" when replies often carry more.
  const named: Array<[string, ReactNode, string]> = [
    ...(hit.video_names ?? []).map((name): [string, ReactNode, string] => ['video', <Video size={12} />, name]),
    ...(hit.pdf_names ?? []).map((name): [string, ReactNode, string] => ['pdf', <FileText size={12} />, name]),
    ...(hit.html_names ?? []).map((name): [string, ReactNode, string] => ['html', <FileCode size={12} />, name]),
    ...(hit.widget_titles ?? []).map((name): [string, ReactNode, string] => ['widget', <AppWindow size={12} />, name]),
  ]
  // A withheld thumbnail (a path the server will not send) still has its count.
  if (!files && !images.length && !named.length && !(hit.image_count ?? 0)) return null
  return (
    <span className="flex flex-wrap items-center gap-1.5 pt-1 text-[12px] text-muted">
      {named.map(([kind, icon, name], i) => <NamedBadge key={`${kind}:${i}`} kind={kind} icon={icon} name={name} />)}
      {images.map(src => (
        <img key={src} src={pinnedImageUrl(src)} alt="" loading="lazy" className="h-12 w-12 rounded object-cover border border-border" />
      ))}
      {(hit.image_count ?? 0) > 0 && (
        <span className="inline-flex items-center gap-1">
          <ImageIcon size={12} aria-hidden />
          {i18nT('components.chatHistorySearch.image_count', { count: hit.image_count ?? 0 })}
        </span>
      )}
      {files > 0 && (
        <span className="inline-flex items-center gap-1">
          <File size={12} aria-hidden />
          {i18nT('components.chatHistorySearch.file_count', { count: files })}
        </span>
      )}
    </span>
  )
}
