import { safeSetItem } from '../../utils/safeStorage'
import { useState, useMemo, useCallback, useEffect, useRef, type ReactNode } from 'react'
import { Ban, Bell, BellOff, Check, CheckCheck, CheckCircle, Layers, Trash2, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useGuardedLeave } from '../NavigationLeaveGuard'
import { useAppSelector, useAppDispatch } from '../../store'
import { deleteNotification, clearNotifications, ackAllNotifications, retireApprovalRow, settleDecidedApproval, approvalDecisionKey, approvalDecideTarget, type RetiredApprovalReason } from '../../store/notificationsSlice'
import { api } from '../../api/client'
import { ApiError, isTerminalApprovalRefusal } from '../../api/apiError'
import { EmptyState, SearchInput } from '../ui'
import Clickable from '../Clickable'
import Glass from '../Glass'
import MarkdownRenderer from '../MarkdownRenderer'
import MessageErrorBoundary from '../MessageErrorBoundary'
import ErrorNotice from '../ErrorNotice'
import { disintegrate, undoDisintegrate } from '../../lib/disintegrate'
import type { Notification } from '../../types'
import {
  parseTs, dateGroup, KIND_META, DEFAULT_META, fmtTime, stripMd, notePriority, safeInternalUrl,
  MAC_ACTION_BTN_CLASS, refusedNotice,
} from './notifMeta'
import NotificationPermissionHint from './NotificationPermissionHint'
import NotificationCard, { CARD_RADIUS, type NotificationCardAction } from './NotificationCard'

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

/**
 * Notification activity feed. Shared by the full page and the topbar bell
 * popover as one implementation: multi-select
 * kind filter (persisted to localStorage), search, ack-all/clear, and a
 * date-grouped list whose rows disintegrate on delete. Selection state is owned
 * by the host (passed via selectedTs/onSelect) so the host renders the matching
 * detail panel; deleting the selected row clears it naturally because the host
 * derives `selected` from the items list by ts.
 */
const NO_RETIRED: Readonly<Record<string, RetiredApprovalReason>> = {}
const NO_DISMISS_FAILED: Readonly<Record<string, true>> = {}

const hasRetired = (map: Readonly<Record<string, RetiredApprovalReason>>, ts: string): boolean =>
  Object.hasOwn(map, ts)

export default function NotificationFeed({ selectedTs, onSelect, variant = 'panel', header, footer, revealTs = null }: {
  selectedTs: string | null
  onSelect: (n: Notification) => void
  /** 'mac' renders rows as floating Notification Center-style cards. */
  variant?: 'panel' | 'mac'
  /** Optional header row rendered inside the mac controls card (title + actions). */
  header?: ReactNode
  /** Optional footer row rendered at the bottom of the mac controls card. */
  footer?: ReactNode
  /** A ts whose row should be brought into view (deep link): expands the
   *  collapsed group_key stack hiding it, then scrolls it into view. Each
   *  distinct value is revealed once; selection/ack stay the host's job. */
  revealTs?: string | null
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
  // Same gate as the detail panel: a note's action button navigates away from
  // whatever page the feed is floating over, and the ask belongs in front of the
  // handler rather than around its navigate call.
  const leave = useGuardedLeave()
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
  const stackedGroups = useMemo(() => {    const out = new Map<string, Row[]>()
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

  // Deep-link reveal. The feed owns this (rather than the host querying the
  // document) because only the feed knows about group_key stacking, and a
  // document-scoped query could hit the same data-ts row rendered by the
  // topbar bell popover. Runs until the row is committed: first pass expands
  // a collapsed stack hiding the target (state change re-runs the effect),
  // the pass that finds the element scrolls it and marks the ts revealed.
  // A row that never renders (silenced behind the muted disclosure, or an
  // unknown ts) is deliberately left alone — reveal is best-effort and the
  // host's detail panel does not depend on it.
  const listRef = useRef<HTMLDivElement>(null)
  const revealedRef = useRef<string | null>(null)
  useEffect(() => {
    if (!revealTs || revealedRef.current === revealTs) return
    const target = items.find(n => n.ts === revealTs)
    if (!target) return
    if (target.group_key) {
      const g = dateGroup(parseTs(target.ts))
      const key = `${g}:${target.group_key}`
      const stackSize = items.filter(n => n.group_key === target.group_key &&
        dateGroup(parseTs(n.ts)) === g).length
      if (stackSize > 1 && !expandedStacks.has(key)) {
        setExpandedStacks(prev => new Set(prev).add(key))
        return
      }
    }
    const el = listRef.current?.querySelector(`[data-ts="${CSS.escape(revealTs)}"]`)
    if (el) {
      revealedRef.current = revealTs
      // Harmless when the host is about to hide the feed (mobile swaps to the
      // full-width detail): the row is still mounted at this point and the
      // scroll is a no-op on a hidden container afterwards.
      el.scrollIntoView?.({ block: 'center' })
    }
  }, [revealTs, items, expandedStacks])
  // One-click approval resolution from the feed. Nothing here removes a row
  // on the decide's own answer: a decision that lands, or a terminal refusal
  // (the approval expired or was decided elsewhere), only RETIRES it in the
  // notifications slice. A retired row
  // stays listed without Approve/Reject and says why, and leaves when a
  // DELETE succeeds (sent here for a landed decision, else by its close X).
  // The marks live in the slice, not here, because this feed mounts twice (the
  // page and the bell popover, which remounts on every open) and both must
  // agree. While a decision this view sent is in flight, that row's
  // Approve/Reject are disabled here, so a slow press does not look dead or
  // invite a second one. Another view deciding the same approval needs no
  // lock: the server accepts one decision per approval and refuses the rest,
  // which retires the row like any refusal. A retryable failure keeps the
  // buttons and says so on that row; the value is
  // the server's own refusal text ('' for a response-less transport failure,
  // which gets the hedged copy), as in `ApprovalCard`. A retired row keeps
  // its ordinary close X, which is how it leaves the feed.
  const [deciding, setDeciding] = useState<ReadonlySet<string>>(() => new Set())
  const dismissFailed = useAppSelector(s => s.notifications.dismissFailed) ?? NO_DISMISS_FAILED
  const [decideFailed, setDecideFailed] = useState<Readonly<Record<string, string>>>({})
  const retiredApprovals = useAppSelector(s => s.notifications.retiredApprovals) ?? NO_RETIRED
  // Retiring a row takes the pressed Approve/Reject away, which drops keyboard
  // focus to <body>. This holds the ts of that approval until its buttons are
  // gone, and the effect below then moves focus to the row's own open control,
  // or to the list once the row has left. Only if focus was actually lost: a
  // reader who has moved on keeps their place.
  const focusRescueRef = useRef<string | null>(null)
  const resolveApprovalNote = useCallback((n: Notification, action: 'approve' | 'reject') => {
    const key = approvalDecisionKey(n)
    const without = <T,>(m: Readonly<Record<string, T>>) => {
      const { [n.ts]: _drop, ...rest } = m
      return rest
    }
    setDecideFailed(without)
    setDeciding(s => new Set(s).add(n.ts))
    // Bound to the request this row showed, when the row names it: the id
    // recurs, and a bare-id decide resolves whichever request holds it now.
    api.resolveApproval(key, action, approvalDecideTarget(n))
      .then(() => {
        focusRescueRef.current = n.ts
        dispatch(settleDecidedApproval(n.ts, action))
      }, err => {
        if (isTerminalApprovalRefusal(err)) {
          focusRescueRef.current = n.ts
          // `refused`, not `gone`: this request failed, so the row shows it
          // as an error rather than as the neutral settled line.
          dispatch(retireApprovalRow(n.ts, 'refused'))
          return
        }
        // eslint-disable-next-line no-console -- keep the raw failure for diagnosis
        console.error(`Inline ${action} failed`, err)
        const reason = err instanceof ApiError && err.message ? err.message : ''
        setDecideFailed(m => ({ ...m, [n.ts]: reason }))
      })
      .finally(() => {
        setDeciding(s => {
          const next = new Set(s)
          next.delete(n.ts)
          return next
        })
      })
  }, [dispatch])
  useEffect(() => {
    const ts = focusRescueRef.current
    if (ts === null) return
    if (items.some(i => i.ts === ts) && !hasRetired(retiredApprovals, ts)) return
    const active = document.activeElement
    const lost = !active || active === document.body
    const row = listRef.current?.querySelector<HTMLElement>(`[data-ts="${CSS.escape(ts)}"]`)
    if (!row) {
      focusRescueRef.current = null
      if (lost) listRef.current?.focus()
      return
    }
    // A landed decision's row leaves once its DELETE confirms, so the rescue
    // stays armed to move focus on to the list then; a refused row stays.
    const why = retiredApprovals[ts]
    if (why !== 'approve' && why !== 'reject') focusRescueRef.current = null
    if (lost) row.querySelector<HTMLElement>('[data-notif-open]')?.focus()
  }, [items, retiredApprovals])

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
        {/* The label names the ACTION the press performs, not the state the rows
            are in: "Muted (3)" left it ambiguous whether pressing reveals muted
            rows or mutes something. Both halves keep the count in parentheses so
            neither needs a plural form. */}
        <BellOff className="lucide-inline" /> {showMuted
          ? i18nT('components.notifications.notificationFeed.hide_muted_count', { count: silencedCount })
          : i18nT('components.notifications.notificationFeed.show_muted_count', { count: silencedCount })}
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

  /** The first-note-from-a-channel prompt: keep or mute. One body, two
   *  shells (an accent glass pane in mac mode, an attached strip in panel mode). */
  const promptBody = (pc: { channel: string; label: string }) => (
    <>
      <Bell className="lucide-inline shrink-0 text-accent" />
      <div className="flex-1 min-w-0 text-[12px] text-text">{i18nT('components.notifications.notificationFeed.first_notification_from')} <span className="font-semibold">{pc.label}</span>{i18nT('components.notifications.notificationFeed.keep_receiving_these')}</div>
      <button
        type="button"
        className="px-2.5 py-1 rounded-md text-[12px] font-semibold cursor-pointer border-none bg-accent text-card hover:opacity-90 transition-opacity font-body whitespace-nowrap"
        onClick={() => markChannelSeen(pc.channel)}
      >{i18nT('components.notifications.notificationFeed.keep')}</button>
      <button
        type="button"
        className="px-2.5 py-1 rounded-md text-[12px] font-medium cursor-pointer bg-transparent text-muted border border-border-strong hover:text-text transition-colors font-body whitespace-nowrap"
        onClick={() => muteChannel(pc.channel)}
      >{i18nT('components.notifications.notificationFeed.mute_channel')}</button>
    </>
  )

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Controls: mac mode groups header + search + muted disclosure in ONE
          floating card (search above the disclosure); panel mode puts the
          disclosure first, directly on the popover surface. */}
      {mac ? (
        <Glass variant="panel" radius={CARD_RADIUS} className="notif-material glass-shadow px-2.5 pt-2 pb-1 mb-2 shrink-0">
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
          {/* One-time nudge toward OS notifications, shown only while the
              browser has not been asked yet and there is something to be
              alerted about. Lives in the controls card so it reads as part
              of the inbox's own settings, not as a notification row. */}
          <NotificationPermissionHint hasNotes={items.length > 0} />
          {footer}
        </Glass>
      ) : (
        <>
          {mutedRow}
          {searchRow}
        </>
      )}

      {/* List. In the mac variant this container is what a press below the
          last card lands on, which is why it carries a test id. Everything
          composed into the mac variant is material or background by decision
          (shell/notifications/notificationSheet.tsx, the sheet's invariant); a new
          child needs one or the other. */}
      <div ref={listRef} tabIndex={-1} data-testid="notification-feed-list" className={`flex-1 overflow-y-auto focus:outline-none ${mac ? 'px-4 -mx-4 pb-2' : 'scroll-shadow'}`}>
        {filtered.length === 0 ? (
          <EmptyState testId="notification-feed-empty" icon={<Bell className="lucide-inline" />} title={i18nT('components.notifications.notificationFeed.no_notifications')} subtitle={filter ? i18nT('components.notifications.notificationFeed.try_a_different_search') : i18nT('components.notifications.notificationFeed.activity_will_appear_here')} />
        ) : (
          Array.from(stackedGroups.entries()).map(([group, rows]) => (
            <div key={group} className="mb-3">
              <div className={mac
                ? 'text-[11px] font-bold text-text-strong/80 uppercase tracking-[.06em] mb-1.5 px-1 drop-shadow-xs'
                : 'text-[11px] font-semibold text-muted uppercase tracking-[.04em] mb-1.5 px-1'}>{group}</div>
              {rows.map(({ n, stackKey, stackCount, stackExpanded, isStackChild }) => {
                const km = KIND_META[n.kind] || DEFAULT_META
                const active = selectedTs === n.ts
                const prio = notePriority(n)
                const silenced = !!n.silenced
                // Priority tiers: passive dims, silenced thins the glass
                // pane's tint (`muted`); critical is signalled by its danger
                // dot alone. Selection is the pane's accent tint step
                // (`active`). The shared card carries `notif-material`:
                // index.css solidifies these surfaces to var(--card) where
                // backdrop-filter is unsupported (#1817).
                // A retired approval asks for nothing, so it drops the
                // critical border and the unread dot and recedes like a read
                // row, whatever its ack flag says while the ack is in flight.
                const settled = hasRetired(retiredApprovals, n.ts)
                const panelBorder = silenced || settled ? 'border-l-muted' : prio === 'critical' ? 'border-l-danger' : km.borderColor
                const promptChannel = promptTs === n.ts && n.channel && n.source
                  ? { channel: n.channel, label: `${n.source} / ${n.channel.startsWith(`${n.source}.`) ? n.channel.slice(n.source.length + 1) : n.channel}` }
                  : null
                // Inline actions: approval approve/reject, plus generic
                // actions that render only with a safe dashboard-internal url
                // (never executable content).
                const isApproval = n.kind === 'approval' && !n.acked && !hasRetired(retiredApprovals, n.ts)
                // A persisted row is untrusted: a truthy non-string body must
                // not reach the renderer, its raw fallback, or the flattener.
                const bodyText = typeof n.body === 'string' ? n.body : ''
                // Defense-in-depth for legacy/corrupted persisted rows: the
                // actions field must be a real array (a truthy non-array like
                // `{}` would throw on .filter), and only string fields render
                // (a non-string label would crash React).
                const urlActions = (Array.isArray(n.actions) ? n.actions : [])
                  .filter(a => typeof a?.id === 'string' && typeof a?.label === 'string' && typeof a?.url === 'string')
                  .map(a => ({ ...a, safeUrl: safeInternalUrl(a.url) }))
                  .filter(a => a.safeUrl)
                const retired = hasRetired(retiredApprovals, n.ts)
                const retiredWhy = retiredApprovals[n.ts]
                const decidingHere = deciding.has(n.ts)
                const approveLabel = i18nT('components.notifications.notificationFeed.approve')
                const rejectLabel = i18nT('components.notifications.notificationFeed.reject')
                const dismissRefused = Object.hasOwn(dismissFailed, n.ts)
                const decideReason = Object.hasOwn(decideFailed, n.ts) && !retired ? decideFailed[n.ts] : null
                const hasActions = isApproval || urlActions.length > 0
                // A row whose controls authorize a command shows the whole
                // command: a clamped excerpt turns `echo safe` + `rm -rf target`
                // into one harmless-looking line. Gated on the KIND, not on
                // unread: reading the row acks it, and a pending command must
                // not collapse back into that line while the detail panel
                // still offers Approve/Reject. A settled approval stays listed
                // only as a retired row, until it is dismissed. Same renderer
                // and boundary as the detail panel; the producer's fence tag
                // makes the lines wrap, so nothing is clipped, clamped or
                // hidden. One definition for both variants: the mac card's
                // own excerpt is a two-line clamp, so it takes this instead.
                const approvalBody = n.kind === 'approval' ? (
                  <div className={`msg-content text-[12px] text-muted break-words ${mac ? 'mt-1 leading-snug' : 'mt-1'}`} data-testid="approval-body">
                    <MessageErrorBoundary rawContent={bodyText}>
                      <MarkdownRenderer content={bodyText} readOnlyCode />
                    </MessageErrorBoundary>
                  </div>
                ) : undefined
                // Outside the row's Clickable in both variants: that control is
                // role="button", whose descendants are presentational, so a
                // notice inside it would never reach a screen reader. The
                // retired line is neutral when the approval settled with
                // nothing failing (a decision that landed, an expiry the
                // server reported); a refused decide or a failed decision
                // renders an error, on the row it failed for. Below the row's
                // controls, as in the detail panel and the chat card, so a
                // retry finds Approve/Reject where they were. No hand-off: the
                // feed also renders in the topbar bell popover, an overlay over
                // the page beneath it, and the hand-off navigates without the
                // `useGuardedLeave` gate.
                const rowNotice = retired || decideReason !== null || dismissRefused ? (
                  <div className={mac ? 'pl-[36px]' : 'pl-6'}>
                    <ErrorNotice
                      variant="inline"
                      className="mt-1"
                      testId="notif-approval-notice"
                      message={decideReason === null ? null
                        : decideReason
                          ? i18nT('components.approvalCard.decision_not_recorded_error', { error: decideReason })
                          : i18nT('components.approvalCard.decision_failed')}
                    />
                    {/* A decide this tab sent was refused (404/410): that request
                        failed, so it renders through the shared ErrorNotice. */}
                    <ErrorNotice
                      variant="inline"
                      className="mt-1"
                      testId="notif-approval-retired"
                      message={retiredWhy === 'refused' ? refusedNotice() : null}
                    />
                    {retired && retiredWhy !== 'refused' && (
                      <div className="mt-1 text-[12px] text-muted" role="status" data-testid="notif-approval-retired">
                        {retiredWhy === 'approve'
                          ? <><CheckCircle className="lucide-inline" /> {i18nT('components.approvalCard.approved')}</>
                          : retiredWhy === 'reject'
                            ? <><Ban className="lucide-inline" /> {i18nT('components.approvalCard.rejected')}</>
                            : i18nT('components.approvalCard.approval_no_longer_pending')}
                      </div>
                    )}
                    {/* A DELETE that failed leaves the row listed: say so on
                        it, whichever view sent the DELETE. */}
                    <ErrorNotice
                      variant="inline"
                      className="mt-1"
                      testId="notif-dismiss-failed"
                      message={dismissRefused ? i18nT('components.notifications.notificationFeed.could_not_dismiss_try_again') : null}
                    />
                  </div>
                ) : null
                const collapsedStack = !!(stackKey && stackCount && stackCount > 1 && !stackExpanded)
                const actionBtn = MAC_ACTION_BTN_CLASS
                // The mac row IS the shared card (one rendering with the
                // banner); the feed adds only what it owns — the reveal anchor,
                // spacing against the deck/prompt below, selection, the stack
                // controls and the ghost/selected material.
                const macActions: NotificationCardAction[] = [
                  ...(isApproval ? [
                    { id: 'approve', label: approveLabel, tone: 'ok' as const, disabled: decidingHere, onClick: () => resolveApprovalNote(n, 'approve') },
                    { id: 'reject', label: rejectLabel, tone: 'danger' as const, disabled: decidingHere, onClick: () => resolveApprovalNote(n, 'reject') },
                  ] : []),
                  ...urlActions.map(a => ({ id: a.id, label: a.label, tone: 'text' as const, onClick: () => leave(() => navigate(a.safeUrl!), a.safeUrl!) })),
                  // Only the quiet "Show less" when expanded; collapse-by-click
                  // lives on the deck.
                  ...(stackKey && stackCount && stackCount > 1 && stackExpanded ? [
                    { id: 'stack', label: i18nT('components.notifications.notificationFeed.show_less'), tone: 'muted' as const, trailing: true, 'aria-expanded': true, onClick: () => toggleStack(stackKey) },
                  ] : []),
                ]
                const dismissRow = async (e?: React.MouseEvent | React.KeyboardEvent) => {
                  const row = (e?.currentTarget as HTMLElement | undefined)?.closest('[data-notif-row]') as HTMLElement | null
                  await disintegrate(row)
                  const r = await dispatch(deleteNotification(n.ts))
                  // Refused: the row stays listed, so bring it back into view
                  // with the notice that says why.
                  if (deleteNotification.rejected.match(r)) undoDisintegrate(row)
                }
                return (
                  <div key={n.ts} className={isStackChild && !mac ? 'ml-4' : ''}>
                    {/* data-ts is the reveal effect's DOM anchor for
                        scroll-into-view, scoped under listRef. */}
                    {mac ? (
                      <NotificationCard
                        data-notif-row data-ts={n.ts}
                        n={n}
                        className={`${collapsedStack ? 'mb-0' : promptChannel ? 'mb-1' : 'mb-2'} ${collapsedStack ? 'relative z-[2] cursor-pointer' : ''}`}
                        active={active}
                        muted={silenced}
                        settled={settled}
                        dismissVisible={settled}
                        onOpen={() => { if (collapsedStack && stackKey) toggleStack(stackKey); else onSelect(n) }}
                        openLabel={collapsedStack
                          ? i18nT('components.notifications.notificationFeed.expand_grouped_notifications', { count: stackCount, title: n.title })
                          : i18nT('components.notifications.notificationFeed.open_notification', { title: n.title })}
                        onDismiss={dismissRow}
                        dismissLabel={i18nT('components.notifications.notificationFeed.dismiss_notification')}
                        actions={macActions}
                        body={approvalBody}
                        footer={rowNotice ?? undefined}
                        trailing={silenced ? (
                          <span className="text-[10px] text-muted italic flex items-center gap-1"><BellOff className="lucide-inline" /> {i18nT('components.notifications.notificationFeed.muted_2')}</span>
                        ) : collapsedStack ? (
                          <span className="text-[10px] font-medium text-muted px-1.5 py-px rounded-full bg-[color-mix(in_srgb,var(--bg-hover)_80%,transparent)]">{stackCount}</span>
                        ) : undefined}
                      />
                    ) : (
                    <div data-notif-row data-ts={n.ts}
                      className={`group flex flex-col px-2.5 py-2 rounded-md ${promptChannel ? 'rounded-b-none mb-0' : 'mb-1'} transition-all border-l-[3px] ${panelBorder} ${silenced ? 'border border-dashed border-border bg-transparent' : active ? 'bg-accent-subtle border border-accent' : 'border border-transparent hover:bg-bg-hover hover:border-border'} ${(n.acked || settled || prio === 'passive') && !active && !silenced ? 'opacity-50' : ''} ${silenced ? 'opacity-60' : ''}`}
                    >
                      <div className="flex items-center gap-2.5">
                      <Clickable
                        data-notif-open
                        onClick={() => onSelect(n)}
                        aria-label={i18nT('components.notifications.notificationFeed.open_notification', { title: n.title })}
                        className="flex items-center gap-2 flex-1 min-w-0 text-left cursor-pointer"
                      >
                        <span className="text-[13px] shrink-0">{km.icon}</span>
                        <div className="flex-1 min-w-0">
                          <div className={`text-[13px] font-semibold truncate leading-tight ${silenced ? 'text-muted font-normal' : 'text-text-strong'}`}>{n.title}</div>
                          {approvalBody ?? (
                            <div className="text-[12px] text-muted mt-0.5 truncate">{stripMd(bodyText).slice(0, 80)}</div>
                          )}
                        </div>
                        <div className="flex flex-col items-end gap-0.5 shrink-0">
                          <span className="text-[11px] text-muted font-mono">{fmtTime(n.ts)}</span>
                          {silenced ? (
                            <span className="text-[10px] text-muted italic flex items-center gap-1"><BellOff className="lucide-inline" /> {i18nT('components.notifications.notificationFeed.muted_2')}</span>
                          ) : !n.acked && !settled ? (
                            <span className={`w-1.5 h-1.5 rounded-full animate-dot-breathe ${prio === 'critical' ? 'bg-danger' : 'bg-accent'}`} data-priority={prio} />
                          ) : null}
                        </div>
                      </Clickable>
                      <Clickable
                        aria-label={i18nT('components.notifications.notificationFeed.dismiss_notification')}
                        className={`${settled ? 'opacity-60' : 'opacity-0 group-hover:opacity-40 [@media(hover:none)]:opacity-60'} focus-visible:opacity-100 text-[11px] cursor-pointer hover:!opacity-100 hover:text-danger transition-opacity shrink-0`}
                        onClick={dismissRow}
                      ><X className="lucide-inline" /></Clickable>
                      </div>
                      {(hasActions || (stackCount && stackCount > 1)) && (
                        <div className="flex items-center gap-1.5 mt-1.5 flex-wrap pl-6">
                          {isApproval && (
                            <>
                              <button
                                type="button"
                                disabled={decidingHere}
                                aria-busy={decidingHere || undefined}
                                className={`${actionBtn} text-ok`}
                                onClick={e => { e.stopPropagation(); resolveApprovalNote(n, 'approve') }}
                              >{approveLabel}</button>
                              <button
                                type="button"
                                disabled={decidingHere}
                                aria-busy={decidingHere || undefined}
                                className={`${actionBtn} text-danger`}
                                onClick={e => { e.stopPropagation(); resolveApprovalNote(n, 'reject') }}
                              >{rejectLabel}</button>
                            </>
                          )}
                          {urlActions.map(a => (
                            <button
                              key={a.id}
                              type="button"
                              className={`${actionBtn} text-text`}
                              onClick={e => { e.stopPropagation(); leave(() => navigate(a.safeUrl!), a.safeUrl!) }}
                            >{a.label}</button>
                          ))}
                          <span className="flex-1" />
                          {/* Panel keeps an explicit stack pill both ways. */}
                          {stackKey && stackCount && stackCount > 1 && (
                            <button
                              type="button"
                              aria-expanded={!!stackExpanded}
                              className="px-2 py-0.5 rounded-full text-[11px] font-medium cursor-pointer bg-bg-hover text-muted border border-border hover:text-text hover:border-border-strong transition-colors font-body whitespace-nowrap"
                              onClick={e => { e.stopPropagation(); toggleStack(stackKey) }}
                            ><Layers className="lucide-inline" /> {stackExpanded ? i18nT('components.notifications.notificationFeed.show_less') : `${stackCount - 1} more`}</button>
                          )}
                        </div>
                      )}
                      {rowNotice}
                    </div>
                    )}
                    {/* macOS NC deck: two card edges peeking below a collapsed
                        stack -- click anywhere on the head to expand. */}
                    {mac && collapsedStack && (
                      <div aria-hidden className="mb-2">
                        {/* The same glass as the head on the faded tint step,
                            tucked under it: only each shell's lower edge shows.
                            Never `opacity` here — it would void the blur. */}
                        <Glass variant="panel" radius={CARD_RADIUS} className="notif-material glass-faded relative z-[1] h-3 -mt-1.5 mx-2" />
                        <Glass variant="panel" radius={CARD_RADIUS} className="notif-material glass-faded relative z-0 h-3 -mt-1.5 mx-4" />
                      </div>
                    )}
                    {promptChannel && (mac ? (
                      // Its own accent-tinted glass under the row (a pane has
                      // one radius, so the strip is not glued to the card).
                      <Glass variant="chip" radius={12} className="notif-material glass-accent glass-shadow flex items-center gap-2 px-3 py-2 mb-2">
                        {promptBody(promptChannel)}
                      </Glass>
                    ) : (
                      <div className="flex items-center gap-2 px-3 py-2 border border-t-0 rounded-b-md mb-1 bg-accent-subtle border-border">
                        {promptBody(promptChannel)}
                      </div>
                    ))}
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
