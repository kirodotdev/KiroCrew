import { safeSetItem } from '../../utils/safeStorage'
import { useState, useMemo, useCallback, type ReactNode } from 'react'
import { Bell, BellOff, Check, CheckCheck, Layers, Trash2, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAppSelector, useAppDispatch } from '../../store'
import { deleteNotification, clearNotifications, ackAllNotifications } from '../../store/notificationsSlice'
import { api } from '../../api/client'
import { EmptyState, SearchInput } from '../ui'
import Clickable from '../Clickable'
import { disintegrate } from '../../lib/disintegrate'
// Aliased: this file defines its own minute-granularity `fmtRelative` wrapper.
import { fmtRelative as fmtRelativeLocalized } from '../../i18n/format'
import type { Notification } from '../../types'
import {
  parseTs, dateGroup, KIND_META, DEFAULT_META, fmtTime, stripMd, notePriority, safeInternalUrl,
} from './notifMeta'

import { i18nT } from '../../i18n/t'
/** localStorage key for app channels the user has already decided on (keep or
 *  mute) via the first-notification prompt. System channels never prompt. */
export const SEEN_CHANNELS_STORAGE_KEY = 'mc:notif:seenChannels'

function loadSeenChannels(): Set<string> {
  try {
    const arr = JSON.parse(localStorage.getItem(SEEN_CHANNELS_STORAGE_KEY) || '[]')
    if (Array.isArray(arr)) return new Set(arr.filter((c): c is string => typeof c === 'string'))
  } catch { /* fall through */ }
  return new Set()
}

/** macOS Notification Center-style relative timestamp ("now", "35m ago", "2h ago").
 *
 * Delegated to the locale-aware seam so relative times render in the app
 * language for all 10 locales, with the "yesterday" literal from CLDR.
 *
 * Minute granularity is preserved deliberately — a notification feed that
 * counted seconds would rewrite every row on every tick. Anything under a
 * minute is collapsed to the locale's "now" rather than "45s ago". */
function fmtRelative(ts: string): string {
  const at = parseTs(ts)
  const now = Date.now()
  if (now - at.getTime() < 60_000) return fmtRelativeLocalized(now, { now })
  return fmtRelativeLocalized(at, { now })
}

/**
 * Notification activity feed. Shared by the full page and the topbar bell
 * popover as one implementation: multi-select
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
  const [filter, setFilter] = useState('')
  // Silenced (muted-channel) rows are ghosts behind an explicit disclosure --
  // mute keeps history but should not clutter the default view. This is NOT a
  // kind filter: it reveals rows that are otherwise unreachable, so it survived
  // the removal of the per-kind chips.
  const [showMuted, setShowMuted] = useState(false)
  // App channels the user has already kept/muted via the first-notification
  // prompt (persisted so the prompt shows exactly once per channel).
  const [seenChannels, setSeenChannels] = useState<Set<string>>(loadSeenChannels)

  const markChannelSeen = useCallback((channel: string) => {
    setSeenChannels(prev => {
      const next = new Set(prev)
      next.add(channel)
      try { safeSetItem(SEEN_CHANNELS_STORAGE_KEY, JSON.stringify(Array.from(next))) } catch { /* ignore quota errors */ }
      return next
    })
  }, [])

  const muteChannel = useCallback((channel: string) => {
    markChannelSeen(channel)
    api.updateNotificationChannelSettings(channel, { muted: true }).catch(() => {})
  }, [markChannelSeen])

  const silencedCount = useMemo(() => items.filter(n => n.silenced).length, [items])

  const filtered = useMemo(() => {
    let list = [...items].reverse()
    // Muted-channel rows stay in history but hide behind the "Show muted"
    // disclosure (mute-doesn't-destroy semantics).
    if (!showMuted) list = list.filter(n => !n.silenced)
    if (filter) {
      const q = filter.toLowerCase()
      list = list.filter(n => ((n.title || '') + (n.body || '')).toLowerCase().includes(q))
    }
    return list
  }, [items, filter, showMuted])

  // First notification from a new app channel gets an inline keep/mute prompt
  // (attached to the newest such row). System channels never prompt; a channel
  // already decided on (or already muted server-side) doesn't either.
  const promptTs = useMemo(() => {
    for (const n of filtered) {
      if (n.source && n.source !== 'system' && n.channel &&
          !seenChannels.has(n.channel) && !n.silenced) return n.ts
    }
    return null
  }, [filtered, seenChannels])

  const groups = useMemo(() => {
    const map = new Map<string, Notification[]>()
    for (const n of filtered) {
      const g = dateGroup(parseTs(n.ts))
      const arr = map.get(g)
      if (arr) arr.push(n); else map.set(g, [n])
    }
    return map
  }, [filtered])

  const navigate = useNavigate()
  // group_key stacking -- notes sharing a group_key within a date group
  // collapse into one stack (newest is the visible head), macOS Notification
  // Center style. Expansion is per stack key, session-local.
  const [expandedStacks, setExpandedStacks] = useState<Set<string>>(new Set())
  const toggleStack = useCallback((key: string) => {
    setExpandedStacks(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })
  }, [])

  type Row = { n: Notification; stackKey?: string; stackCount?: number; stackExpanded?: boolean; isStackChild?: boolean }
  const stackedGroups = useMemo(() => {
    const out = new Map<string, Row[]>()
    for (const [g, notes] of groups.entries()) {
      const rows: Row[] = []
      const stacks = new Map<string, Notification[]>()
      for (const n of notes) {
        if (!n.group_key) continue
        const arr = stacks.get(n.group_key)
        if (arr) arr.push(n); else stacks.set(n.group_key, [n])
      }
      const seen = new Set<string>()
      for (const n of notes) {
        if (!n.group_key || (stacks.get(n.group_key)?.length ?? 0) < 2) {
          rows.push({ n })
          continue
        }
        if (seen.has(n.group_key)) continue
        seen.add(n.group_key)
        const stack = stacks.get(n.group_key)!  // notes is newest-first, so [0] is the head
        const stackKey = `${g}:${n.group_key}`
        const expanded = expandedStacks.has(stackKey)
        rows.push({ n: stack[0], stackKey, stackCount: stack.length, stackExpanded: expanded })
        if (expanded) for (const child of stack.slice(1)) rows.push({ n: child, isStackChild: true })
      }
      out.set(g, rows)
    }
    return out
  }, [groups, expandedStacks])

  // One-click approval resolution from the feed.
  const resolveApprovalNote = useCallback((n: Notification, action: 'approve' | 'reject') => {
    api.resolveApproval(n.approval_id || n.ts, action)
      .then(() => { dispatch(deleteNotification(n.ts)) })
      // eslint-disable-next-line no-console -- intentional failure diagnostic;
      // the row stays in the feed and remains retryable (detail panel too).
      .catch(err => { console.error(`Inline ${action} failed`, err) })
  }, [dispatch])

  const unread = items.filter(n => !n.acked).length
  const mac = variant === 'mac'

  // Extracted so the two variants can order them differently: panel puts the
  // muted disclosure above the search box, mac puts it below, inside one grouped
  // card (with the host-provided header on top).
  //
  // The muted row renders solely when something is actually silenced, so it
  // disappears entirely on a normal feed.
  const mutedRow = silencedCount > 0 ? (
    <div className={`flex gap-1 ${mac ? 'mb-1.5' : 'mb-2'} flex-wrap shrink-0`}>
      <button
        type="button"
        aria-pressed={showMuted}
        className={`px-2 py-1 rounded-md text-[12px] font-medium cursor-pointer border border-dashed transition-all font-body ${showMuted ? 'bg-bg-hover text-text border-border-strong' : 'bg-transparent text-muted border-border hover:text-text hover:border-border-strong'}`}
        onClick={() => setShowMuted(v => !v)}
      >
        <BellOff className="lucide-inline" /> {i18nT('components.notifications.notificationFeed.muted_count', { count: silencedCount })}
      </button>
    </div>
  ) : null
  const searchRow = (
    <div className="flex gap-2 mb-2 items-center shrink-0">
      <div className="flex-1"><SearchInput className="[&>input]:!bg-bg-elevated/40 [&>input]:!border-border/60" placeholder={i18nT('components.notifications.notificationFeed.search')} value={filter} onChange={e => setFilter(e.target.value)} /></div>
      {!mac && unread > 0 && <button className="px-2 py-1 rounded-md border border-ok/40 bg-ok/10 text-ok text-[12px] font-semibold cursor-pointer hover:bg-ok/20 transition-all font-body whitespace-nowrap" onClick={() => dispatch(ackAllNotifications())}><Check className="lucide-inline" /> {i18nT('components.notifications.notificationFeed.all')}</button>}
      {!mac && items.length > 0 && <button className="px-2 py-1 rounded-md border border-danger/40 bg-transparent text-danger text-[12px] font-medium cursor-pointer hover:bg-danger/10 transition-all font-body whitespace-nowrap" onClick={() => { if (confirm(i18nT('components.notifications.notificationFeed.clear_all_notifications'))) dispatch(clearNotifications()) }}><X className="lucide-inline" /> {i18nT('components.notifications.notificationFeed.clear')}</button>}
    </div>
  )

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Controls: mac mode groups header + search + muted disclosure in ONE
          floating card (search above the disclosure); panel mode puts the
          disclosure first, directly on the popover surface. */}
      {mac ? (
        <div className="rounded-2xl bg-[color-mix(in_srgb,var(--card)_55%,transparent)] backdrop-blur-2xl backdrop-saturate-150 shadow-[0_8px_24px_rgba(0,0,0,.10),0_1px_3px_rgba(0,0,0,.06)] border border-[color-mix(in_srgb,var(--border)_55%,transparent)] px-2.5 pt-2 pb-1 mb-2 shrink-0">
          <div className="flex items-center gap-1.5">
            <div className="flex-1 min-w-0">{header}</div>
            {unread > 0 && (
              <button
                title={i18nT('components.notifications.notificationFeed.mark_all_as_read')}
                aria-label={i18nT('components.notifications.notificationFeed.mark_all_as_read')}
                className="w-6 h-6 rounded-md flex items-center justify-center text-ok bg-transparent border-none cursor-pointer hover:bg-ok/10 transition-colors shrink-0"
                onClick={() => dispatch(ackAllNotifications())}
              ><CheckCheck className="lucide-inline" /></button>
            )}
            {items.length > 0 && (
              <button
                title={i18nT('components.notifications.notificationFeed.clear_all_notifications')}
                aria-label={i18nT('components.notifications.notificationFeed.clear_all_notifications')}
                className="w-6 h-6 rounded-md flex items-center justify-center text-muted bg-transparent border-none cursor-pointer hover:bg-danger/10 hover:text-danger transition-colors shrink-0"
                onClick={() => { if (confirm(i18nT('components.notifications.notificationFeed.clear_all_notifications'))) dispatch(clearNotifications()) }}
              ><Trash2 className="lucide-inline" /></button>
            )}
          </div>
          {searchRow}
          {mutedRow}
          {footer}
        </div>
      ) : (
        <>
          {mutedRow}
          {searchRow}
        </>
      )}

      {/* List */}
      <div className={`flex-1 overflow-y-auto ${mac ? 'px-4 -mx-4 pb-2' : 'scroll-shadow'}`}>
        {filtered.length === 0 ? (
          <EmptyState testId="notification-feed-empty" icon={<Bell className="lucide-inline" />} title={i18nT('components.notifications.notificationFeed.no_notifications')} subtitle={filter ? i18nT('components.notifications.notificationFeed.try_a_different_search') : i18nT('components.notifications.notificationFeed.activity_will_appear_here')} />
        ) : (
          Array.from(stackedGroups.entries()).map(([group, rows]) => (
            <div key={group} className="mb-3">
              <div className={mac
                ? 'text-[11px] font-bold text-text-strong/80 uppercase tracking-[.06em] mb-1.5 px-1 drop-shadow-sm'
                : 'text-[11px] font-semibold text-muted uppercase tracking-[.04em] mb-1.5 px-1'}>{group}</div>
              {rows.map(({ n, stackKey, stackCount, stackExpanded, isStackChild }) => {
                const km = KIND_META[n.kind] || DEFAULT_META
                const active = selectedTs === n.ts
                const prio = notePriority(n)
                const silenced = !!n.silenced
                // Priority tiers: critical gets a danger edge, passive dims,
                // silenced renders as a dashed-border ghost.
                const macCard = silenced
                  ? 'bg-[color-mix(in_srgb,var(--card)_35%,transparent)] backdrop-blur-xl border border-dashed border-[color-mix(in_srgb,var(--border)_70%,transparent)]'
                  : `bg-[color-mix(in_srgb,var(--card)_55%,transparent)] backdrop-blur-2xl backdrop-saturate-150 shadow-[0_8px_24px_rgba(0,0,0,.10),0_1px_3px_rgba(0,0,0,.06)] ${active ? 'border border-accent bg-accent-subtle' : 'border border-[color-mix(in_srgb,var(--border)_55%,transparent)] hover:bg-[color-mix(in_srgb,var(--card)_70%,transparent)]'}`
                const panelBorder = silenced ? 'border-l-muted' : prio === 'critical' ? 'border-l-danger' : km.borderColor
                const contentDim = silenced ? 'opacity-50' : (n.acked && !active) || prio === 'passive' ? (mac ? 'opacity-55' : '') : ''
                const promptChannel = promptTs === n.ts && n.channel && n.source
                  ? { channel: n.channel, label: `${n.source} / ${n.channel.startsWith(`${n.source}.`) ? n.channel.slice(n.source.length + 1) : n.channel}` }
                  : null
                // Inline actions: approval approve/reject, plus generic
                // actions that render only with a safe dashboard-internal url
                // (never executable content).
                const isApproval = n.kind === 'approval' && !n.acked
                // Defense-in-depth for legacy/corrupted persisted rows: the
                // actions field must be a real array (a truthy non-array like
                // `{}` would throw on .filter), and only string fields render
                // (a non-string label would crash React).
                const urlActions = (Array.isArray(n.actions) ? n.actions : [])
                  .filter(a => typeof a?.id === 'string' && typeof a?.label === 'string' && typeof a?.url === 'string')
                  .map(a => ({ ...a, safeUrl: safeInternalUrl(a.url) }))
                  .filter(a => a.safeUrl)
                const hasActions = isApproval || urlActions.length > 0
                const collapsedStack = !!(stackKey && stackCount && stackCount > 1 && !stackExpanded)
                // macOS NC action buttons: quiet translucent capsules, text-only,
                // semantic tint on the LABEL (never a solid colored fill).
                const actionBtn = 'px-3 py-1 rounded-lg text-[12px] font-medium cursor-pointer font-body whitespace-nowrap transition-colors bg-[color-mix(in_srgb,var(--bg-hover)_80%,transparent)] backdrop-blur border border-[color-mix(in_srgb,var(--border)_45%,transparent)] hover:bg-bg-hover'
                return (
                  <div key={n.ts} className={isStackChild && !mac ? 'ml-4' : ''}>
                    <div data-notif-row
                      className={mac
                        ? `group flex flex-col px-3 py-2.5 rounded-2xl ${promptChannel || collapsedStack ? 'mb-0' : 'mb-2'} ${promptChannel ? 'rounded-b-none' : ''} ${collapsedStack ? 'relative z-[2] cursor-pointer' : ''} transition-all ${macCard}`
                        : `group flex flex-col px-2.5 py-2 rounded-md ${promptChannel ? 'rounded-b-none mb-0' : 'mb-1'} transition-all border-l-[3px] ${panelBorder} ${silenced ? 'border border-dashed border-border bg-transparent' : active ? 'bg-accent-subtle border border-accent' : 'border border-transparent hover:bg-bg-hover hover:border-border'} ${(n.acked || prio === 'passive') && !active && !silenced ? 'opacity-50' : ''} ${silenced ? 'opacity-60' : ''}`}
                    >
                      <div className={`flex ${mac ? 'items-start' : 'items-center'} gap-2.5`}>
                      <Clickable
                        onClick={() => { if (mac && collapsedStack && stackKey) toggleStack(stackKey); else onSelect(n) }}
                        aria-label={mac && collapsedStack
                          ? i18nT('components.notifications.notificationFeed.expand_grouped_notifications', { count: stackCount, title: n.title })
                          : i18nT('components.notifications.notificationFeed.open_notification', { title: n.title })}
                        className={`flex ${mac ? 'items-start' : 'items-center'} gap-2 flex-1 min-w-0 text-left cursor-pointer ${mac ? contentDim : ''}`}
                      >
                        {mac ? (
                          <span className={`w-8 h-8 rounded-[9px] flex items-center justify-center shrink-0 text-[14px] ${km.color}`}>{km.icon}</span>
                        ) : (
                          <span className="text-[13px] shrink-0">{km.icon}</span>
                        )}
                        <div className="flex-1 min-w-0">
                          <div className={`text-[13px] font-semibold truncate leading-tight ${silenced ? 'text-muted font-normal' : 'text-text-strong'}`}>{n.title}</div>
                          <div className={`text-[12px] text-muted mt-0.5 ${mac ? 'line-clamp-2 leading-snug' : 'truncate'}`}>{stripMd(n.body || '').slice(0, mac ? 140 : 80)}</div>
                        </div>
                        <div className="flex flex-col items-end gap-0.5 shrink-0">
                          <span className={`text-[11px] text-muted ${mac ? '' : 'font-mono'}`}>{mac ? fmtRelative(n.ts) : fmtTime(n.ts)}</span>
                          {silenced ? (
                            <span className="text-[10px] text-muted italic flex items-center gap-1"><BellOff className="lucide-inline" /> {i18nT('components.notifications.notificationFeed.muted_2')}</span>
                          ) : !n.acked ? (
                            <span className={`w-1.5 h-1.5 rounded-full animate-dot-breathe ${prio === 'critical' ? 'bg-danger' : 'bg-accent'}`} data-priority={prio} />
                          ) : null}
                          {mac && collapsedStack && (
                            <span className="text-[10px] font-medium text-muted px-1.5 py-px rounded-full bg-[color-mix(in_srgb,var(--bg-hover)_80%,transparent)]">{stackCount}</span>
                          )}
                        </div>
                      </Clickable>
                      <Clickable
                        aria-label={i18nT('components.notifications.notificationFeed.dismiss_notification')}
                        className="opacity-0 group-hover:opacity-40 text-[11px] cursor-pointer hover:!opacity-100 hover:text-danger transition-opacity shrink-0"
                        onClick={async e => { e?.stopPropagation(); const row = (e?.currentTarget as HTMLElement | undefined)?.closest('[data-notif-row]') as HTMLElement | null; await disintegrate(row); dispatch(deleteNotification(n.ts)) }}
                      ><X className="lucide-inline" /></Clickable>
                      </div>
                      {(hasActions || (!mac && stackCount && stackCount > 1) || (mac && stackExpanded)) && (
                        <div className={`flex items-center gap-1.5 mt-1.5 flex-wrap ${mac ? 'pl-[42px]' : 'pl-6'}`}>
                          {isApproval && (
                            <>
                              <button
                                type="button"
                                className={`${actionBtn} text-ok`}
                                onClick={e => { e.stopPropagation(); resolveApprovalNote(n, 'approve') }}
                              >{i18nT('components.notifications.notificationFeed.approve')}</button>
                              <button
                                type="button"
                                className={`${actionBtn} text-danger`}
                                onClick={e => { e.stopPropagation(); resolveApprovalNote(n, 'reject') }}
                              >{i18nT('components.notifications.notificationFeed.reject')}</button>
                            </>
                          )}
                          {urlActions.map(a => (
                            <button
                              key={a.id}
                              type="button"
                              className={`${actionBtn} text-text`}
                              onClick={e => { e.stopPropagation(); navigate(a.safeUrl!) }}
                            >{a.label}</button>
                          ))}
                          <span className="flex-1" />
                          {/* Stack toggle: mac shows only the quiet "Show less"
                              when expanded (collapse-by-click lives on the deck);
                              panel keeps an explicit pill both ways. */}
                          {stackKey && stackCount && stackCount > 1 && (mac ? stackExpanded : true) && (
                            <button
                              type="button"
                              aria-expanded={!!stackExpanded}
                              className={mac
                                ? `${actionBtn} text-muted`
                                : 'px-2 py-0.5 rounded-full text-[11px] font-medium cursor-pointer bg-bg-hover text-muted border border-border hover:text-text hover:border-border-strong transition-colors font-body whitespace-nowrap'}
                              onClick={e => { e.stopPropagation(); toggleStack(stackKey) }}
                            >{mac ? i18nT('components.notifications.notificationFeed.show_less') : <><Layers className="lucide-inline" /> {stackExpanded ? i18nT('components.notifications.notificationFeed.show_less') : `${stackCount - 1} more`}</>}</button>
                          )}
                        </div>
                      )}
                    </div>
                    {/* macOS NC deck: two card edges peeking below a collapsed
                        stack -- click anywhere on the head to expand. */}
                    {mac && collapsedStack && (
                      <div aria-hidden className="mb-2">
                        <div className={`relative z-[1] h-3 -mt-1.5 mx-2 rounded-b-2xl ${silenced ? 'bg-[color-mix(in_srgb,var(--card)_30%,transparent)]' : 'bg-[color-mix(in_srgb,var(--card)_45%,transparent)]'} backdrop-blur-xl border border-t-0 border-[color-mix(in_srgb,var(--border)_45%,transparent)] shadow-[0_4px_12px_rgba(0,0,0,.06)]`} />
                        <div className="relative z-0 h-3 -mt-1.5 mx-4 rounded-b-2xl bg-[color-mix(in_srgb,var(--card)_35%,transparent)] backdrop-blur-lg border border-t-0 border-[color-mix(in_srgb,var(--border)_35%,transparent)]" />
                      </div>
                    )}
                    {promptChannel && (
                      <div className={`flex items-center gap-2 px-3 py-2 border border-t-0 ${mac
                        ? 'rounded-b-2xl mb-2 bg-accent-subtle backdrop-blur-2xl border-[color-mix(in_srgb,var(--border)_55%,transparent)]'
                        : 'rounded-b-md mb-1 bg-accent-subtle border-border'}`}>
                        <Bell className="lucide-inline shrink-0 text-accent" />
                        <div className="flex-1 min-w-0 text-[12px] text-text">{i18nT('components.notifications.notificationFeed.first_notification_from')} <span className="font-semibold">{promptChannel.label}</span>{i18nT('components.notifications.notificationFeed.keep_receiving_these')}</div>
                        <button
                          type="button"
                          className="px-2.5 py-1 rounded-md text-[12px] font-semibold cursor-pointer border-none bg-accent text-card hover:opacity-90 transition-opacity font-body whitespace-nowrap"
                          onClick={() => markChannelSeen(promptChannel.channel)}
                        >{i18nT('components.notifications.notificationFeed.keep')}</button>
                        <button
                          type="button"
                          className="px-2.5 py-1 rounded-md text-[12px] font-medium cursor-pointer bg-transparent text-muted border border-border-strong hover:text-text transition-colors font-body whitespace-nowrap"
                          onClick={() => muteChannel(promptChannel.channel)}
                        >{i18nT('components.notifications.notificationFeed.mute_channel')}</button>
                      </div>
                    )}
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
