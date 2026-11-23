/** Search all chats: every matching message across the user's chats.
 *
 *  Opened with ⌘⇧F / Ctrl+Shift+F or the sidebar button. Results come from
 *  `GET /api/sessions/search/messages`, grouped by chat, and can be narrowed by
 *  bot, speaker and date. Choosing a message opens its chat (the same resume the
 *  sidebar and the palette use) and asks the chat page to scroll to that message
 *  through `pendingMessageJump`, which pages back to an older message as the
 *  pins' jump does. */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Bot, MessageSquare, Search, User } from 'lucide-react'

import { api } from '../../api/client'
import type { MessageSearchHit, MessageSearchParams, MessageSearchResult } from '../../api/client'
import { SEARCH_MIN_CHARS } from '../../api/client/sessions'
import { useAppDispatch, useAppStore } from '../../store'
import { resumeFromHistory } from '../../store/chatSlice'
import { focusComposerForResumedSession } from '../../pages/chat/composerFocus'
import { requestMessageJump } from '../../lib/pendingMessageJump'
import { useDebouncedValue } from '../../hooks/useDebouncedValue'
import { useImeGuard } from '../../hooks/useImeGuard'
import { i18nT } from '../../i18n/t'
import { timeAgo } from '../../utils/timeAgo'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { Dialog, DialogContent, DialogTitle } from '../ui/dialog'
import SimpleSelect from '../SimpleSelect'
import ErrorNotice from '../ErrorNotice'
import { useGuardedLeave } from '../NavigationLeaveGuard'
import { closeShownCrewWindow } from '../../pages/chat/crew-window/crewWindowStore'
import { closeGlobalChatSearch, useGlobalChatSearchOpen } from './globalChatSearchStore'
import { highlightRanges } from './highlight'

export type WhenPreset = 'any' | 'today' | 'week' | 'month'
type Who = '' | 'user' | 'assistant'

const DEBOUNCE_MS = 250
const ANY = '__any__'
const DAY_S = 86_400

/** The `since` a date preset asks for, in epoch seconds, from *nowMs*. */
export function sinceFor(preset: WhenPreset, nowMs: number): number | undefined {
  if (preset === 'any') return undefined
  if (preset === 'today') {
    const start = new Date(nowMs)
    start.setHours(0, 0, 0, 0)
    return Math.floor(start.getTime() / 1000)
  }
  return Math.floor(nowMs / 1000) - (preset === 'week' ? 7 : 30) * DAY_S
}

interface FlatHit { result: MessageSearchResult; hit: MessageSearchHit }

export default function GlobalChatSearchDialog() {
  const open = useGlobalChatSearchOpen()
  return (
    <Dialog open={open} onOpenChange={next => { if (!next) closeGlobalChatSearch() }}>
      {open && <GlobalChatSearchBody />}
    </Dialog>
  )
}

function GlobalChatSearchBody() {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const ime = useImeGuard()
  const [q, setQ] = useState('')
  const [agent, setAgent] = useState('')
  const [who, setWho] = useState<Who>('')
  const [when, setWhen] = useState<WhenPreset>('any')
  const [active, setActive] = useState(0)
  const [openError, setOpenError] = useState(false)
  const seenAgents = useRef(new Set<string>())
  const listRef = useRef<HTMLDivElement>(null)
  const openingRef = useRef(false)
  // False once this body unmounts (the dialog was dismissed): an open still
  // pending then must not close, navigate or jump on behalf of a later dialog.
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const query = useDebouncedValue(q.trim(), DEBOUNCE_MS)
  // Read once per settled query, so a "Today" search started before midnight
  // keeps asking for the same day.
  const since = useMemo(() => sinceFor(when, Date.now()), [when, query]) // eslint-disable-line react-hooks/exhaustive-deps
  const params: MessageSearchParams = { q: query, agent: agent || undefined, role: who || undefined, since }
  const enabled = query.length >= SEARCH_MIN_CHARS
  const { data, isFetching, isError } = useQuery({
    queryKey: ['global-chat-search', params],
    queryFn: ({ signal }) => api.sessionsSearchMessages(params, signal),
    enabled,
    staleTime: 15_000,
  })
  const results = useMemo(() => (enabled && data ? data.results : []), [enabled, data])
  // Every Bot the ranking matched, not only the listed rows', so a Bot whose chats
  // rank lower can still be picked; kept across queries so a choice stays listed.
  for (const a of (enabled && data?.agents) || []) seenAgents.current.add(a)
  for (const r of results) if (r.agent) seenAgents.current.add(r.agent)
  const agents = [...seenAgents.current].sort()
  const flat: FlatHit[] = useMemo(
    () => results.flatMap(result => result.hits.map(hit => ({ result, hit }))),
    [results],
  )
  useEffect(() => { setActive(0) }, [flat])
  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' })
  }, [active])

  const openHit = (item: FlatHit | undefined) => {
    // One open at a time: the dialog stays up while the resume runs, and the
    // thunk drops a repeat as a rejection, which would read as a failed open.
    if (!item || openingRef.current) return
    // Asked before the resume, as the ⌘⇧T reopen asks: a page holding unsaved
    // work keeps it unless the user agrees to leave, and nothing is resumed then.
    guardedLeave(() => startOpen(item), '/chat')
  }

  const startOpen = (item: FlatHit) => {
    openingRef.current = true
    const { result, hit } = item
    setOpenError(false)
    // Leaving Split View makes the resumed chat the one on screen, so the jump lands.
    const resume = dispatch(resumeFromHistory({ key: result.key, title: result.title || result.key, leaveSplit: true })).unwrap()
    focusComposerForResumedSession(resume, store)
    resume.then(payload => {
      openingRef.current = false
      if (!mountedRef.current) return
      // Closed only once the resume answers: a failed open is told here, in the
      // dialog, which would already be gone had it closed on the click.
      closeGlobalChatSearch()
      navigate('/chat')
      // A resume that did not put this chat on screen (the user switched away
      // first) requests nothing: a jump left pending would fire on a later visit.
      if (!payload.ok || store.getState().chat.activeSlot !== payload.key) return
      // A crew window covering this chat stays up when the chat was already the
      // active one (it closes only on a switch), and would hide the hit; close it
      // the way its own Close button does.
      closeShownCrewWindow()
      if (!hit.ts) return
      requestMessageJump({ slotKey: payload.key, ts: hit.ts, mid: hit.mid })
    }, () => { openingRef.current = false; if (mountedRef.current) setOpenError(true) })
  }

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') { if (ime.claimEnter(e)) openHit(flat[active]); return }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    // A candidate list the IME draws owns the arrows while it is open.
    if (!ime.claimKey(e)) return
    e.preventDefault()
    const down = e.key === 'ArrowDown'
    setActive(i => (down ? Math.min(i + 1, flat.length - 1) : Math.max(i - 1, 0)))
  }

  let status: string | null = null
  // An empty field shows the same hint as a short one, so the list area is never blank.
  if (!enabled) status = i18nT('components.globalChatSearch.hint_min', { min: SEARCH_MIN_CHARS })
  else if (isError) status = null
  else if (isFetching && !data) status = i18nT('components.globalChatSearch.searching')
  else if (data && results.length === 0) status = i18nT('components.globalChatSearch.no_results')

  let index = -1
  return (
    <DialogContent maxWidth={720}>
      <div className="sr-only"><DialogTitle>{i18nT('components.globalChatSearch.title')}</DialogTitle></div>
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <Search className="lucide-inline text-muted shrink-0" aria-hidden />
        <input
          autoFocus
          value={q}
          onChange={e => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          {...ime.bindComposition()}
          placeholder={i18nT('components.globalChatSearch.placeholder')}
          aria-label={i18nT('components.globalChatSearch.title')}
          aria-controls="global-chat-search-results"
          className="flex-1 min-w-0 bg-transparent text-sm py-1"
        />
      </div>
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-border text-[12px]">
        <FilterSelect
          label={i18nT('components.globalChatSearch.filter_bot')}
          value={agent}
          onChange={setAgent}
          options={[['', i18nT('components.globalChatSearch.any_bot')], ...agents.map(a => [a, a] as [string, string])]}
        />
        <FilterSelect
          label={i18nT('components.globalChatSearch.filter_who')}
          value={who}
          onChange={v => setWho(v as Who)}
          options={[
            ['', i18nT('components.globalChatSearch.who_any')],
            ['user', i18nT('components.globalChatSearch.who_me')],
            ['assistant', i18nT('components.globalChatSearch.who_bot')],
          ]}
        />
        <FilterSelect
          label={i18nT('components.globalChatSearch.filter_when')}
          value={when}
          onChange={v => setWhen(v as WhenPreset)}
          options={[
            ['any', i18nT('components.globalChatSearch.when_any')],
            ['today', i18nT('components.globalChatSearch.when_today')],
            ['week', i18nT('components.globalChatSearch.when_week')],
            ['month', i18nT('components.globalChatSearch.when_month')],
          ]}
        />
      </div>
      <div
        id="global-chat-search-results"
        ref={listRef}
        role="listbox"
        aria-label={i18nT('components.globalChatSearch.results_label')}
        className="flex-1 min-h-0 overflow-y-auto py-1"
      >
        {status && <p role="status" className="px-4 py-6 text-center text-[13px] text-muted">{status}</p>}
        {enabled && isError && (
          <div className="px-3 py-2">
            <ErrorNotice
              variant="inline"
              message={i18nT('components.globalChatSearch.error')}
              askAgent
              onHandoff={closeGlobalChatSearch}
              testId="global-chat-search-error"
            />
          </div>
        )}
        {openError && (
          <div className="px-3 py-2">
            <ErrorNotice
              variant="inline"
              message={i18nT('components.globalChatSearch.open_failed')}
              askAgent
              onHandoff={closeGlobalChatSearch}
              testId="global-chat-search-open-error"
            />
          </div>
        )}
        {results.map(result => (
          <section key={result.key} className="px-2 pb-1">
            <header className="flex items-center gap-2 px-2 pt-2 pb-1 text-[12px] text-muted">
              <MessageSquare className="lucide-inline shrink-0" aria-hidden />
              <span className="font-medium text-text truncate">{sessionDisplayTitle({ key: result.key, title: result.title })}</span>
              {result.agent && <span className="truncate">{result.agent}</span>}
              {typeof result.modified === 'number' && <span className="ml-auto shrink-0">{timeAgo(result.modified)}</span>}
            </header>
            {result.hits.map(hit => {
              index += 1
              const mine = index
              const isActive = mine === active
              return (
                <button
                  type="button"
                  key={`${result.key}:${hit.index}`}
                  role="option"
                  aria-selected={isActive}
                  data-active={isActive}
                  onMouseEnter={() => setActive(mine)}
                  onClick={() => openHit({ result, hit })}
                  className={`w-full flex items-start gap-2 text-left rounded-md px-2 py-1.5 text-[13px] cursor-pointer ${isActive ? 'bg-accent-subtle text-text' : 'hover:bg-bg-hover'}`}
                >
                  {hit.role === 'user'
                    ? <User className="lucide-inline mt-0.5 shrink-0 text-muted" aria-label={i18nT('components.globalChatSearch.role_you')} />
                    : <Bot className="lucide-inline mt-0.5 shrink-0 text-muted" aria-label={i18nT('components.globalChatSearch.role_bot')} />}
                  <span className="min-w-0 break-words">{highlightRanges(hit.snippet, query)}</span>
                </button>
              )
            })}
            {result.hit_count > result.hits.length && (
              <p className="px-8 pb-1 text-[11px] text-muted">
                {i18nT('components.globalChatSearch.more_matches', { count: result.hit_count - result.hits.length })}
              </p>
            )}
          </section>
        ))}
      </div>
      <footer className="flex items-center gap-3 px-3 py-1.5 border-t border-border text-[11px] text-muted">
        <span>{i18nT('components.globalChatSearch.keyboard_hint')}</span>
        {data?.scan && (
          <span className="ml-auto">
            {i18nT('components.globalChatSearch.scan_note', { searched: data.scan.searched, total: data.scan.total })}
          </span>
        )}
      </footer>
    </DialogContent>
  )
}

function FilterSelect({ label, value, onChange, options }: {
  label: string
  value: string
  onChange: (value: string) => void
  options: Array<[string, string]>
}) {
  // A Radix select item cannot carry an empty value, so "no filter" travels as ANY.
  const wire = (v: string) => (v === '' ? ANY : v)
  return (
    <span className="flex items-center gap-1">
      <span className="text-muted" aria-hidden>{label}</span>
      <SimpleSelect
        value={wire(value)}
        onChange={v => onChange(v === ANY ? '' : v)}
        options={options.map(([v]) => wire(v))}
        optionLabels={options.map(([, text]) => text)}
        aria-label={label}
      />
    </span>
  )
}
