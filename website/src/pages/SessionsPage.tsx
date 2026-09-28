import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Plus, MessagesSquare } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../store'
import { createSlot } from '../store/chatSlice'
import { readPinnedSessionOrder } from '../utils/pinnedSessionOrder'
import { api } from '../api/client'
import { isChatPageSurface } from '../utils/channelOrigin'
import { timeAgo } from '../utils/timeAgo'
import { lastActivityEpoch, localDaysAgo } from './chat/sessionOrder'
import { relativeLuminance } from '../lib/iconContrast'
import { errMessage } from '../utils/thunkError'
import { PageHeader, Btn, SearchInput, Badge, EmptyState, FilteredEmpty } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import type { ChatSlot, ChatTag } from '../types'
import { i18nT } from '../i18n/t'

/** Recency bucket a session row sorts into, by LOCAL calendar day. */
export type RecencyGroup = 'today' | 'yesterday' | 'earlier'

/** Bucket a timestamp against local-midnight boundaries, reusing the shared
 *  DST-safe day-index math the sidebar's recency tint and date segments use, so
 *  a row cannot land in a different bucket here than it would in the sidebar. */
export function recencyGroup(tsMs: number, nowMs: number): RecencyGroup {
  const days = localDaysAgo(new Date(nowMs), new Date(tsMs))
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  return 'earlier'
}

/** Chip filter state: 'all', 'unread', or 'tag:<name>' for a status tag. */
type ChipFilter = 'all' | 'unread' | `tag:${string}`

/** Black or white foreground for an arbitrary hex background, by WCAG luminance. */
function avatarFg(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return '#000'
  const n = parseInt(m[1], 16)
  return relativeLuminance((n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff) > 0.4 ? '#000' : '#fff'
}

/** Render order: pinned sessions surface first — pinning is a reachability
 * promise (see utils/pinnedSessionOrder.ts), so a parked pinned session must
 * stay findable above the recency buckets. */
type PageGroup = 'pinned' | RecencyGroup
const GROUP_ORDER: PageGroup[] = ['pinned', 'today', 'yesterday', 'earlier']

/**
 * Top-level `/sessions` page: a neutral, bookmarkable session chooser with no
 * session selected. Rows navigate to the full `/chat/<key>` experience; the
 * page renders inside the main dashboard shell, so the nav rail and top bar
 * stay available. Unlike bare `/chat` it never auto-selects or auto-creates a
 * session — that is the point of the page.
 */
export default function SessionsPage() {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const slots = useAppSelector(s => s.dashboard.slots)
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  const [query, setQuery] = useState('')
  const [chip, setChip] = useState<ChipFilter>('all')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const { data: tags = [], error: tagsError } = useQuery<ChatTag[]>({
    queryKey: ['chat-tags'],
    queryFn: () => api.chatTags(),
  })

  // Same visibility rule as the chat sidebar: chat-like surfaces only.
  const visible = useMemo(
    () => slots.filter(s => isChatPageSurface(s.surface ?? s.mode)),
    [slots],
  )
  const unread = useMemo(() => new Set(unreadSlots), [unreadSlots])
  // Badge and filter must count the same population: unreadSlots can hold
  // non-chat-surface and orphaned keys (see filterUnreadKeysBySurface), so the
  // raw store count can exceed the rows the Unread chip reveals.
  const unreadVisible = useMemo(
    () => visible.filter(s => unread.has(s.key)).length,
    [visible, unread],
  )

  // Status-tag chips are offered only for tags actually in use, in vocabulary
  // order, so the chip row never fills with a stale taxonomy. slot.tags holds
  // tag IDs (see ChatSidebar's t.id matching); names are display-only.
  const statusChips = useMemo(() => {
    const inUse = new Set(visible.flatMap(s => s.tags ?? []))
    return tags.filter(t => t.status && inUse.has(t.id)).sort((a, b) => a.order - b.order)
  }, [tags, visible])
  const tagById = useMemo(() => new Map(tags.map(t => [t.id, t])), [tags])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return visible.filter(s => {
      if (chip === 'unread' && !unread.has(s.key)) return false
      if (chip.startsWith('tag:') && !(s.tags ?? []).includes(chip.slice(4))) return false
      if (q && !(s.title ?? s.key).toLowerCase().includes(q)) return false
      return true
    })
  }, [visible, chip, query, unread])

  const groups = useMemo(() => {
    const now = Date.now()
    const by: Record<PageGroup, ChatSlot[]> = { pinned: [], today: [], yesterday: [], earlier: [] }
    // Pinned slots surface in their own group; unpinned bucket by local day.
    for (const s of filtered) by[s.pinned ? 'pinned' : recencyGroup(lastActivityEpoch(s) * 1000, now)].push(s)
    for (const g of GROUP_ORDER) by[g].sort((a, b) => lastActivityEpoch(b) - lastActivityEpoch(a))
    // The sidebar's pinned section is MANUALLY ordered (pinnedSessionOrder.ts);
    // honor the same user-arranged order here so the two surfaces agree.
    // Keys missing from the stored order keep the recency sort, after ranked ones.
    const rank = new Map(readPinnedSessionOrder().map((k, i) => [k, i]))
    by.pinned.sort((a, b) => (rank.get(a.key) ?? Infinity) - (rank.get(b.key) ?? Infinity))
    return by
  }, [filtered])

  const groupLabel: Record<PageGroup, string> = {
    // Same label the sidebar's pinned filter uses, so the two surfaces agree.
    pinned: i18nT('pages.chatSidebar.filter_pinned'),
    today: i18nT('pages.sessionsPage.group_today'),
    yesterday: i18nT('pages.sessionsPage.group_yesterday'),
    earlier: i18nT('pages.sessionsPage.group_earlier'),
  }

  const onNew = async () => {
    if (creating) return
    setCreating(true)
    setCreateError(null)
    try {
      const slot = await dispatch(createSlot(undefined)).unwrap()
      if (slot?.key) navigate(`/chat?sid=${encodeURIComponent(slot.key)}`)
    } catch (err) {
      // errMessage, not `err instanceof Error`: unwrap() throws a serialized
      // plain object, and the instanceof idiom renders '[object Object]'
      // (see utils/thunkError.ts).
      const detail = errMessage(err)
      // In-page recovery surface (errors-use-error-notice): the ErrorNotice
      // below the header owns the failed state — the sole report, per the
      // first-principles review (no parallel notification dispatch).
      setCreateError(detail)
    } finally {
      setCreating(false)
    }
  }

  const chipCls = (active: boolean) =>
    `text-[13px] px-3 py-1 rounded-full border transition-colors cursor-pointer ${
      active
        ? 'bg-accent-subtle border-accent text-accent'
        : 'border-border-strong text-muted hover:border-[var(--border-hover)]'
    }`

  return (
    <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden" data-testid="sessions-page">
      <div className="pt-3">
        <PageHeader
          title={i18nT('pages.sessionsPage.page_title')}
          actions={
            <Btn primary onClick={onNew} disabled={creating} data-testid="sessions-new">
              <Plus size={15} strokeWidth={2.5} />
              {i18nT('pages.sessionsPage.new_session')}
            </Btn>
          }
        />
      </div>
      {/* This page holds no draft input, so the agent hand-off loses nothing
          (see ErrorNotice's askAgent contract). Tags failing only degrades the
          chip row; sessions themselves still render from the slots stream. */}
      {(createError || tagsError) && (
        <div className="px-4 md:px-6 pb-2 flex flex-col gap-2">
          <ErrorNotice
            title={i18nT('pages.chatPage.could_not_start_a_new_session')}
            message={createError}
            onDismiss={() => setCreateError(null)}
            askAgent
            testId="sessions-create-error"
          />
          <ErrorNotice
            message={tagsError ? errMessage(tagsError) : null}
            askAgent
            testId="sessions-tags-error"
          />
        </div>
      )}
      <div className="flex items-center gap-2 px-4 md:px-6 pb-2">
        <SearchInput
          type="search"
          aria-label={i18nT('pages.sessionsPage.search_placeholder')}
          placeholder={i18nT('pages.sessionsPage.search_placeholder')}
          value={query}
          onChange={e => setQuery(e.target.value)}
          data-testid="sessions-search"
          className="flex-1 max-w-[420px]"
        />
      </div>
      <div className="flex items-center gap-2 flex-wrap px-4 md:px-6 pb-3">
        <button className={chipCls(chip === 'all')} aria-pressed={chip === 'all'} onClick={() => setChip('all')}>
          {i18nT('pages.sessionsPage.filter_all')}
        </button>
        <button
          className={chipCls(chip === 'unread')}
          aria-pressed={chip === 'unread'}
          onClick={() => setChip(chip === 'unread' ? 'all' : 'unread')}
          data-testid="sessions-chip-unread"
        >
          {i18nT('pages.sessionsPage.filter_unread')}
          {unreadVisible > 0 && <span className="ml-1">{unreadVisible}</span>}
        </button>
        {statusChips.map(t => (
          <button
            key={t.id}
            className={chipCls(chip === `tag:${t.id}`)}
            aria-pressed={chip === `tag:${t.id}`}
            onClick={() => setChip(chip === `tag:${t.id}` ? 'all' : `tag:${t.id}`)}
            data-testid={`sessions-chip-tag-${t.id}`}
          >
            {t.name}
          </button>
        ))}
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto pb-8 md:px-6">
        {!slotsLoaded && visible.length === 0 ? (
          // An empty frame before the first SSE slots payload is ambiguous (the
          // store keeps `slotsLoaded` precisely to disambiguate it, as ChatSidebar
          // does), so show skeleton rows rather than flash "No sessions" and a
          // create CTA at a user who actually has sessions still loading.
          <div className="md:px-1 pt-3" data-testid="sessions-loading" aria-hidden="true">
            <div className="md:border md:border-border md:rounded-xl md:overflow-hidden md:bg-bg-elevated/30">
              {[0, 1, 2, 3, 4].map(i => (
                <div
                  key={i}
                  className={`flex items-center gap-3 px-4 py-2.5 border-border ${i < 4 ? 'border-b' : 'border-b md:border-b-0'}`}
                >
                  <span className="w-8 h-8 rounded-lg bg-bg-hover shrink-0 animate-pulse" />
                  <span className="flex-1 min-w-0 space-y-1.5">
                    <span className="block h-3 w-40 max-w-[60%] rounded bg-bg-hover animate-pulse" />
                    <span className="block h-2.5 w-24 max-w-[40%] rounded bg-bg-hover animate-pulse" />
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : visible.length === 0 ? (
          <EmptyState
            icon={<MessagesSquare size={40} />}
            title={i18nT('pages.sessionsPage.empty_title')}
            subtitle={i18nT('pages.sessionsPage.empty_subtitle')}
            action={
              <Btn primary onClick={onNew} disabled={creating} data-testid="sessions-empty-new">
                <Plus size={15} strokeWidth={2.5} />
                {i18nT('pages.sessionsPage.new_session')}
              </Btn>
            }
          />
        ) : filtered.length === 0 ? (
          <FilteredEmpty
            // A chip-only zero result has an empty query; echo the active chip's
            // label instead so the message never quotes an empty string.
            query={
              query.trim() ||
              (chip === 'unread'
                ? i18nT('pages.sessionsPage.filter_unread')
                : (tagById.get(chip.slice(4))?.name ?? ''))
            }
            onClear={() => {
              setQuery('')
              setChip('all')
            }}
          />
        ) : (
          GROUP_ORDER.filter(g => groups[g].length > 0).map(g => (
            <section key={g} aria-label={groupLabel[g]}>
              <div className="px-4 md:px-1 pt-3 pb-1.5 text-[11px] uppercase tracking-wider font-semibold text-muted-strong select-none">
                {groupLabel[g]}
              </div>
              {/* Full-bleed divider list on phone; thin-bordered rounded card
                  (schedule-table style) on sm+. */}
              <div className="md:border md:border-border md:rounded-xl md:overflow-hidden md:bg-bg-elevated/30">
                {groups[g].map((s, i) => (
                  <button
                    key={s.key}
                    className={`w-full flex items-center gap-3 px-4 py-2.5 text-left cursor-pointer hover:bg-bg-hover border-border ${
                      i < groups[g].length - 1 ? 'border-b' : 'border-b md:border-b-0'
                    }`}
                    onClick={() => navigate(`/chat?sid=${encodeURIComponent(s.key)}`)}
                    data-testid={`sessions-row-${s.key}`}
                  >
                    <span
                      className="w-8 h-8 rounded-lg bg-bg-hover text-text-strong text-sm font-semibold flex items-center justify-center shrink-0 uppercase"
                      style={s.color_hex ? { background: s.color_hex, color: avatarFg(s.color_hex) } : undefined}
                    >
                      {(s.agent || s.title || s.key).charAt(0)}
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className="block text-[13.5px] font-medium text-text-strong truncate">
                        {s.title || s.key}
                      </span>
                      <span className="block text-[12px] text-muted truncate">
                        {s.agent}
                        {(s.tags ?? []).map(id => {
                          const t = tagById.get(id)
                          if (!t) return null
                          return (
                            <Badge
                              key={id}
                              variant="muted"
                              className="ml-1.5 text-[12px] py-0"
                              style={{ color: t.color }}
                            >
                              {t.name}
                            </Badge>
                          )
                        })}
                      </span>
                    </span>
                    {unread.has(s.key) && (
                      <span
                        className="w-2.5 h-2.5 rounded-full bg-accent shrink-0"
                        data-testid={`sessions-unread-${s.key}`}
                      >
                        <span className="sr-only">{i18nT('pages.sessionsPage.filter_unread')}</span>
                      </span>
                    )}
                    <span className="text-[11px] text-muted-strong shrink-0">
                      {timeAgo(lastActivityEpoch(s))}
                    </span>
                  </button>
                ))}
              </div>
            </section>
          ))
        )}
      </div>
    </div>
  )
}
