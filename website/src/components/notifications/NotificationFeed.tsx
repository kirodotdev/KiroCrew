import { safeSetItem } from '../../utils/safeStorage'
import { useState, useMemo, useCallback, useEffect, type ReactNode } from 'react'
import { Bell, Check, CheckCheck, Trash2, X } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { deleteNotification, clearNotifications, ackAllNotifications } from '../../store/notificationsSlice'
import { EmptyState, SearchInput } from '../ui'
import Clickable from '../Clickable'
import { disintegrate } from '../../lib/disintegrate'
import type { Notification } from '../../types'
import {
  type Kind, type Category, KIND_KEYS, CATEGORIES, KINDS_STORAGE_KEY, loadActiveKinds,
  parseTs, dateGroup, KIND_META, DEFAULT_META, fmtTime, stripMd,
} from './notifMeta'

/** macOS Notification Center-style relative timestamp ("now", "35m ago", "2h ago"). */
function fmtRelative(ts: string): string {
  const mins = Math.floor((Date.now() - parseTs(ts).getTime()) / 60_000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return days === 1 ? 'yesterday' : `${days}d ago`
}

/**
 * Notification activity feed. Extracted verbatim from NotificationsPage so the
 * full page and the topbar bell popover share one implementation: multi-select
 * kind filter (persisted to localStorage), search, ack-all/clear, and a
 * date-grouped list whose rows disintegrate on delete. Selection state is owned
 * by the host (passed via selectedTs/onSelect) so the host renders the matching
 * detail panel; deleting the selected row clears it naturally because the host
 * derives `selected` from the items list by ts.
 */
export default function NotificationFeed({ selectedTs, onSelect, variant = 'panel', header, footer }: {
  selectedTs: string | null
  onSelect: (n: Notification) => void
  /** 'mac' renders rows as floating Notification Center-style cards. */
  variant?: 'panel' | 'mac'
  /** Optional header row rendered inside the mac controls card (title + actions). */
  header?: ReactNode
  /** Optional footer row rendered at the bottom of the mac controls card. */
  footer?: ReactNode
}) {
  const dispatch = useAppDispatch()
  const items = useAppSelector(s => s.notifications.items)
  const [activeKinds, setActiveKinds] = useState<Set<Kind>>(loadActiveKinds)
  const [filter, setFilter] = useState('')

  const allActive = activeKinds.size === KIND_KEYS.length
  const noneActive = activeKinds.size === 0

  // Persist filter selection across reloads
  useEffect(() => {
    try { safeSetItem(KINDS_STORAGE_KEY, JSON.stringify(Array.from(activeKinds))) } catch { /* ignore quota errors */ }
  }, [activeKinds])

  const toggleCategory = useCallback((key: Category) => {
    if (key === 'all') {
      // "All" is a meta-toggle: if everything is on, clear; otherwise select all.
      setActiveKinds(prev => prev.size === KIND_KEYS.length ? new Set<Kind>() : new Set<Kind>(KIND_KEYS))
      return
    }
    setActiveKinds(prev => {
      const next = new Set<Kind>(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const filtered = useMemo(() => {
    let list = [...items].reverse()
    // When every known kind is selected, behave like the old "All" state and
    // include notifications with unknown kinds too. Otherwise filter strictly.
    if (!allActive) list = list.filter(n => activeKinds.has(n.kind as Kind))
    if (filter) {
      const q = filter.toLowerCase()
      list = list.filter(n => ((n.title || '') + (n.body || '')).toLowerCase().includes(q))
    }
    return list
  }, [items, activeKinds, allActive, filter])

  const groups = useMemo(() => {
    const map = new Map<string, Notification[]>()
    for (const n of filtered) {
      const g = dateGroup(parseTs(n.ts))
      const arr = map.get(g)
      if (arr) arr.push(n); else map.set(g, [n])
    }
    return map
  }, [filtered])

  const unread = items.filter(n => !n.acked).length
  const mac = variant === 'mac'

  // Extracted so the two variants can order them differently: panel keeps the
  // original chips-then-search order; mac renders search-then-chips inside one
  // grouped card (with the host-provided header on top).
  const chipsRow = (
    <div className={`flex gap-1 ${mac ? 'mb-1.5' : 'mb-2'} flex-wrap shrink-0`} role="group" aria-label="Filter notifications by kind">
      {CATEGORIES.map(c => {
        const isActive = c.key === 'all' ? allActive : activeKinds.has(c.key as Kind)
        return (
          <button
            key={c.key}
            type="button"
            aria-pressed={isActive}
            title={c.key === 'all' ? (allActive ? 'Clear all filters' : 'Select all categories') : `Toggle ${c.label}`}
            className={`px-2 py-1 rounded-md text-[12px] font-medium cursor-pointer border transition-all font-body ${isActive ? 'bg-accent-subtle text-accent border-accent' : 'bg-transparent text-muted border-border hover:text-text hover:border-border-strong'}`}
            onClick={() => toggleCategory(c.key)}
          >
            {c.icon} {c.label}
          </button>
        )
      })}
    </div>
  )
  const searchRow = (
    <div className="flex gap-2 mb-2 items-center shrink-0">
      <div className="flex-1"><SearchInput className="[&>input]:!bg-bg-elevated/40 [&>input]:!border-border/60" placeholder="Search…" value={filter} onChange={e => setFilter(e.target.value)} /></div>
      {!mac && unread > 0 && <button className="px-2 py-1 rounded-md border border-ok/40 bg-ok/10 text-ok text-[12px] font-semibold cursor-pointer hover:bg-ok/20 transition-all font-body whitespace-nowrap" onClick={() => dispatch(ackAllNotifications())}><Check className="lucide-inline" /> All</button>}
      {!mac && items.length > 0 && <button className="px-2 py-1 rounded-md border border-danger/40 bg-transparent text-danger text-[12px] font-medium cursor-pointer hover:bg-danger/10 transition-all font-body whitespace-nowrap" onClick={() => { if (confirm('Clear all notifications?')) dispatch(clearNotifications()) }}><X className="lucide-inline" /> Clear</button>}
    </div>
  )

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Controls: mac mode groups header + search + filter chips in ONE
          floating card (search above chips); panel mode keeps the original
          chips-then-search order directly on the popover surface. */}
      {mac ? (
        <div className="rounded-2xl bg-[color-mix(in_srgb,var(--card)_55%,transparent)] backdrop-blur-2xl backdrop-saturate-150 shadow-[0_8px_24px_rgba(0,0,0,.10),0_1px_3px_rgba(0,0,0,.06)] border border-[color-mix(in_srgb,var(--border)_55%,transparent)] px-2.5 pt-2 pb-1 mb-2 shrink-0">
          <div className="flex items-center gap-1.5">
            <div className="flex-1 min-w-0">{header}</div>
            {unread > 0 && (
              <button
                title="Mark all as read"
                aria-label="Mark all as read"
                className="w-6 h-6 rounded-md flex items-center justify-center text-ok bg-transparent border-none cursor-pointer hover:bg-ok/10 transition-colors shrink-0"
                onClick={() => dispatch(ackAllNotifications())}
              ><CheckCheck className="lucide-inline" /></button>
            )}
            {items.length > 0 && (
              <button
                title="Clear all notifications"
                aria-label="Clear all notifications"
                className="w-6 h-6 rounded-md flex items-center justify-center text-muted bg-transparent border-none cursor-pointer hover:bg-danger/10 hover:text-danger transition-colors shrink-0"
                onClick={() => { if (confirm('Clear all notifications?')) dispatch(clearNotifications()) }}
              ><Trash2 className="lucide-inline" /></button>
            )}
          </div>
          {searchRow}
          {chipsRow}
          {footer}
        </div>
      ) : (
        <>
          {chipsRow}
          {searchRow}
        </>
      )}

      {/* List */}
      <div className={`flex-1 overflow-y-auto ${mac ? 'px-4 -mx-4 pb-2' : 'scroll-shadow'}`}>
        {filtered.length === 0 ? (
          <EmptyState icon={<Bell className="lucide-inline" />} title="No notifications" subtitle={noneActive ? 'No categories selected — click a category above' : filter ? 'Try a different search' : 'Activity will appear here'} />
        ) : (
          Array.from(groups.entries()).map(([group, notes]) => (
            <div key={group} className="mb-3">
              <div className={mac
                ? 'text-[11px] font-bold text-text-strong/80 uppercase tracking-[.06em] mb-1.5 px-1 drop-shadow-sm'
                : 'text-[11px] font-semibold text-muted uppercase tracking-[.04em] mb-1.5 px-1'}>{group}</div>
              {notes.map(n => {
                const km = KIND_META[n.kind] || DEFAULT_META
                const active = selectedTs === n.ts
                return (
                  <div key={n.ts} data-notif-row
                    className={mac
                      ? `group flex items-start gap-2.5 px-3 py-2.5 rounded-2xl mb-2 transition-all bg-[color-mix(in_srgb,var(--card)_55%,transparent)] backdrop-blur-2xl backdrop-saturate-150 shadow-[0_8px_24px_rgba(0,0,0,.10),0_1px_3px_rgba(0,0,0,.06)] ${active ? 'border border-accent bg-accent-subtle' : 'border border-[color-mix(in_srgb,var(--border)_55%,transparent)] hover:bg-[color-mix(in_srgb,var(--card)_70%,transparent)]'}`
                      : `group flex items-center gap-2 px-2.5 py-2 rounded-md mb-1 transition-all border-l-[3px] ${km.borderColor} ${active ? 'bg-accent-subtle border border-accent' : 'border border-transparent hover:bg-bg-hover hover:border-border'} ${n.acked && !active ? 'opacity-50' : ''}`}
                  >
                    <Clickable
                      onClick={() => onSelect(n)}
                      aria-label={`Open notification: ${n.title}`}
                      className={`flex ${mac ? 'items-start' : 'items-center'} gap-2 flex-1 min-w-0 text-left cursor-pointer ${mac && n.acked && !active ? 'opacity-55' : ''}`}
                    >
                      {mac ? (
                        <span className={`w-8 h-8 rounded-[9px] flex items-center justify-center shrink-0 text-[14px] ${km.color}`}>{km.icon}</span>
                      ) : (
                        <span className="text-[13px] shrink-0">{km.icon}</span>
                      )}
                      <div className="flex-1 min-w-0">
                        <div className="text-[13px] font-semibold text-text-strong truncate leading-tight">{n.title}</div>
                        <div className={`text-[12px] text-muted mt-0.5 ${mac ? 'line-clamp-2 leading-snug' : 'truncate'}`}>{stripMd(n.body || '').slice(0, mac ? 140 : 80)}</div>
                      </div>
                      <div className="flex flex-col items-end gap-0.5 shrink-0">
                        <span className={`text-[11px] text-muted ${mac ? '' : 'font-mono'}`}>{mac ? fmtRelative(n.ts) : fmtTime(n.ts)}</span>
                        {!n.acked && <span className="w-1.5 h-1.5 rounded-full bg-accent animate-dot-breathe" />}
                      </div>
                    </Clickable>
                    <Clickable
                      aria-label="Dismiss notification"
                      className="opacity-0 group-hover:opacity-40 text-[11px] cursor-pointer hover:!opacity-100 hover:text-danger transition-opacity shrink-0"
                      onClick={async e => { e?.stopPropagation(); const row = (e?.currentTarget as HTMLElement | undefined)?.closest('[data-notif-row]') as HTMLElement | null; await disintegrate(row); dispatch(deleteNotification(n.ts)) }}
                    ><X className="lucide-inline" /></Clickable>
                  </div>
                )
              })}
            </div>
          ))
        )}
      </div>
    </div>
  )
}
