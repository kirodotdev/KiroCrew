/**
 * Mochi — Watchlist side panel (list + detail mode)
 *
 * Visual design inspired by Linear's compact issue list:
 * - Left accent bar for priority (high=danger, normal=accent, low=muted)
 * - Single-row layout: label left, countdown right
 * - Status as colored dot, not text
 * - Action buttons appear on hover only
 * - Detail page: pill badges for kind/status, grouped sections
 */
import React, { useEffect, useState, useCallback, useRef } from 'react'
import { ArrowLeft, Eye, X } from 'lucide-react'
import { WATCHLIST_PANEL_WIDTH } from '../shared/constants'
import { TERMINAL_STATUSES, MIN_CHECK_INTERVAL_MINS } from '../shared/watchlistTypes'
import type { WatchItem, WatchPriority } from '../shared/watchlistTypes'
import { api } from '../mochiApi'
import { DEFAULT_PET_NAME } from '../../builtinPacks'
import { i18nT } from '../../../../i18n/t'
import { watchKindLabel, watchPriorityLabel, watchStatusLabel } from '../../i18nKeys'
import { fmtDateFields } from '../../../../i18n/format'

// ── Editable fields type (exported for testing) ────────────────────────────

export interface EditableFields {
  notes: string
  priority: WatchPriority
  checkIntervalMins: number
}

export const EDITABLE_FIELD_KEYS: ReadonlySet<string> = new Set(['notes', 'priority', 'checkIntervalMins'])

// ── Pure helper functions (exported for testing) ───────────────────────────

/**
 * Countdown phrasings, keyed by a discriminant rather than returned as the key itself:
 * `check-i18n-keys.mjs` resolves only file-scope bindings, so a key carried on a local
 * object is one it cannot verify exists in the catalog. Indexing this map at the two
 * render sites checks all three keys.
 */
const COUNTDOWN_KEY = {
  overdue: 'apps.mochi.watchPanel.overdue',
  mins: 'apps.mochi.watchPanel.countdown.mins',
  hours: 'apps.mochi.watchPanel.countdown.hours',
} as const

export type CountdownKind = keyof typeof COUNTDOWN_KEY

export function formatCountdown(
  nextCheckAfter: string,
  now: Date,
): { kind: CountdownKind; params?: Record<string, string> } {
  const diff = new Date(nextCheckAfter).getTime() - now.getTime()
  if (diff <= 0) return { kind: 'overdue' }
  const mins = Math.ceil(diff / 60_000)
  if (mins < 60) return { kind: 'mins', params: { mins: String(mins) } }
  const hours = Math.floor(mins / 60)
  const remainMins = mins % 60
  return { kind: 'hours', params: { hours: String(hours), mins: String(remainMins) } }
}

export function formatStatusSummary(
  item: WatchItem,
): string {
  const statusName = watchStatusLabel(item.status)
  if (item.lastResult) return `${statusName} — ${item.lastResult}`
  return statusName
}

export function buildEditPayload(draft: Partial<EditableFields>): Partial<EditableFields> {
  const payload: Partial<EditableFields> = {}
  for (const [k, v] of Object.entries(draft)) {
    if (EDITABLE_FIELD_KEYS.has(k) && v !== undefined) (payload as any)[k] = v
  }
  return payload
}

export function applyDataRefresh(
  newItems: WatchItem[], selectedItemId: string | null, editDraft: Partial<EditableFields> | null,
): { selectedItemId: string | null; editDraft: Partial<EditableFields> | null } {
  if (selectedItemId && !newItems.some(i => i.id === selectedItemId)) return { selectedItemId: null, editDraft: null }
  return { selectedItemId, editDraft }
}

export function escapeNavigation(selectedItemId: string | null): { action: 'to-list' } | { action: 'close' } {
  return selectedItemId ? { action: 'to-list' } : { action: 'close' }
}

function formatRelativeTime(isoStr: string, now: Date): string {
  const diff = now.getTime() - new Date(isoStr).getTime()
  if (diff < 0) return i18nT('apps.mochi.relativeTime.just_now')
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return i18nT('apps.mochi.relativeTime.just_now')
  if (mins < 60) return i18nT('apps.mochi.relativeTime.minutes', { count: mins })
  const hours = Math.floor(mins / 60)
  if (hours < 24) return i18nT('apps.mochi.relativeTime.hours', { count: hours })
  return i18nT('apps.mochi.relativeTime.days', { count: Math.floor(hours / 24) })
}

// ── Visual helpers ─────────────────────────────────────────────────────────



// ── Component ──────────────────────────────────────────────────────────────

export interface WatchlistPanelProps {
  visible: boolean
  onClose: () => void
  /** ADDED (not upstream): the empty hint names the PET, which is renameable. */
  petName?: string
}

export const WatchlistPanel: React.FC<WatchlistPanelProps> = ({ visible, onClose, petName }) => {
  const [items, setItems] = useState<WatchItem[]>([])
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState<Partial<EditableFields> | null>(null)
  const [showAllHistory, setShowAllHistory] = useState(false)
  const [showCompleted, setShowCompleted] = useState(false)
  const [intervalUnit, setIntervalUnit] = useState<'min' | 'hr' | 'day'>('min')
  const [loading, setLoading] = useState(true)
  /**
   * Clearing completed items is a permanent bulk delete with no undo, so it is
   * confirmed rather than fired on one click of 10px text. `failed` exists
   * because the request can be rejected: the old code dropped the items locally
   * and never checked the response, so a failure read as success until the next
   * refresh brought everything back.
   */
  const [clearState, setClearState] = useState<'idle' | 'confirm' | 'working' | 'failed'>('idle')

  /**
   * Terminal (done/cancelled/failed) items. Component scope, not local to the
   * list renderer, because the clear-completed CONFIRM also needs the count —
   * and restating the predicate in two places is how the two would drift.
   */
  const terminalItems = items.filter(i => TERMINAL_STATUSES.includes(i.status))
  // Animation: track previous view for transitions
  const [viewAnim, setViewAnim] = useState<'idle' | 'to-detail' | 'to-list'>('idle')
  const animTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!visible) return
    let cancelled = false
    // Do NOT blank the list to re-open. The panel never unmounts (it animates
    // between width 0 and full), so `items` still holds the last good data — and
    // flipping `loading` back on replaced a correct list with the "..."
    // placeholder for one round trip, which is the flash the user sees on every
    // expand. Stale-while-revalidate: keep showing what we have, and only claim
    // to be loading when there is genuinely nothing to show.
    setLoading((prev) => prev && items.length === 0)
    api?.getWatchlist?.().then((data: WatchItem[]) => {
      if (!cancelled) { setItems(data || []); setLoading(false) }
    }).catch(() => { if (!cancelled) { setItems([]); setLoading(false) } })
    return () => { cancelled = true }
    // `items` is deliberately NOT a dependency: this must run on open, not on
    // every data change (the WS subscriber below owns those).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible])

  useEffect(() => {
    if (!visible) return
    const off = api?.onWatchlistChanged?.((newItems: WatchItem[]) => {
      setItems(newItems || [])
      setSelectedItemId(prev => {
        const result = applyDataRefresh(newItems || [], prev, null)
        if (result.selectedItemId === null && prev !== null) setEditDraft(null)
        return result.selectedItemId
      })
    })
    return () => { off?.() }
  }, [visible])

  const handleSetStatus = useCallback((id: string, status: 'done' | 'cancelled' | 'watching') => {
    if (status === 'watching') {
      setItems(prev => prev.map(item =>
        item.id === id ? { ...item, status: 'watching', completedAt: undefined } as WatchItem : item
      ))
    } else {
      setItems(prev => prev.map(item =>
        item.id === id ? { ...item, status, completedAt: new Date().toISOString() } : item
      ))
    }
    api?.setWatchItemStatus?.(id, status)
  }, [])

  useEffect(() => {
    if (!visible) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // The confirm overlay is the innermost layer, so it consumes Escape
        // first. Without this, dismissing the dialog also closed the panel
        // behind it — two layers unwound by one keystroke.
        if (clearState === 'confirm' || clearState === 'failed') {
          setClearState('idle')
          return
        }
        const nav = escapeNavigation(selectedItemId)
        if (nav.action === 'to-list') { setSelectedItemId(null); setEditDraft(null) }
        else onClose()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [visible, selectedItemId, onClose, clearState])

  // Panel always renders (width transitions between 0 and WATCHLIST_PANEL_WIDTH)

  const now = new Date()

  const selectItem = (id: string) => {
    setViewAnim('to-detail')
    if (animTimer.current) clearTimeout(animTimer.current)
    animTimer.current = setTimeout(() => setViewAnim('idle'), 250)
    setSelectedItemId(id)
    setEditDraft({})
    setShowAllHistory(false)
    const item = items.find(i => i.id === id)
    if (item) {
      const mins = item.checkIntervalMins
      if (mins >= 1440 && mins % 1440 === 0) setIntervalUnit('day')
      else if (mins >= 60 && mins % 60 === 0) setIntervalUnit('hr')
      else setIntervalUnit('min')
    } else setIntervalUnit('min')
  }

  const updateDraft = (field: keyof EditableFields, value: string | number) => {
    setEditDraft(prev => ({ ...prev, [field]: value }))
  }

  const handleSave = () => {
    if (!selectedItemId || !editDraft || Object.keys(editDraft).length === 0) return
    const payload = buildEditPayload(editDraft)
    if (Object.keys(payload).length === 0) return
    api?.updateWatchItem?.(selectedItemId, payload)
    setItems(prev => prev.map(item => {
      if (item.id !== selectedItemId) return item
      const updated = { ...item, ...payload }
      if (payload.checkIntervalMins !== undefined) {
        updated.nextCheckAfter = new Date(Date.now() + payload.checkIntervalMins * 60_000).toISOString()
      }
      return updated
    }))
    setEditDraft({})
  }

  const backToList = () => {
    setViewAnim('to-list')
    if (animTimer.current) clearTimeout(animTimer.current)
    animTimer.current = setTimeout(() => setViewAnim('idle'), 250)
    setSelectedItemId(null)
    setEditDraft(null)
  }
  const selectedItem = selectedItemId ? items.find(i => i.id === selectedItemId) : null
  const hasDraftChanges = editDraft !== null && Object.keys(editDraft).length > 0

  // ── List mode ───────────────────────────────────────────────────────────

  const renderListMode = () => {
    const activeItems = items.filter(i => !TERMINAL_STATUSES.includes(i.status))
    const oneDayAgo = now.getTime() - 24 * 60 * 60 * 1000
    const recentTerminal = terminalItems.filter(i => i.completedAt && new Date(i.completedAt).getTime() > oneDayAgo)
    const olderTerminal = terminalItems.filter(i => !i.completedAt || new Date(i.completedAt).getTime() <= oneDayAgo)

    const renderItem = (item: WatchItem) => {
      const isTerminal = TERMINAL_STATUSES.includes(item.status)
      const countdown = !isTerminal && item.nextCheckAfter ? formatCountdown(item.nextCheckAfter, now) : null
      const isOverdue = countdown?.kind === 'overdue'

      return (
        <div
          key={item.id}
          tabIndex={0}
          role="button"
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectItem(item.id) }
          }}
          onClick={() => selectItem(item.id)}
          style={{
            cursor: 'pointer',
            padding: '8px 12px',
            borderBottom: '1px solid rgba(255,255,255,0.08)',
            opacity: isTerminal ? 0.4 : 1,
          }}
        >
          {/* Row 1: label + countdown */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <span style={{
              fontSize: 13, fontWeight: 500, color: 'var(--text)',
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1,
              letterSpacing: -0.2,
            }}>
              {item.label}
            </span>
            {countdown && (
              <span style={{
                fontSize: 11, flexShrink: 0,
                color: isOverdue ? 'var(--danger)' : 'var(--text-muted)',
                opacity: isOverdue ? 1 : 0.6,
              }}>
                {i18nT(COUNTDOWN_KEY[countdown.kind], countdown.params)}
              </span>
            )}
          </div>

          {/* Row 2: kind pill + status text */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{
              display: 'inline-block', padding: '1px 6px', borderRadius: 4,
              fontSize: 10, fontWeight: 500,
              background: 'rgba(255,255,255,0.06)',
              color: 'var(--text-muted)',
            }}>{watchKindLabel(item.kind)}</span>
            <span style={{
              fontSize: 11, color: 'var(--text-muted)', opacity: 0.6,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1,
            }}>
              {formatStatusSummary(item)}
            </span>
          </div>

          {/* Row 3: hover actions — opacity + height transition */}
          {!isTerminal && (
            <div className="wl-actions" role="presentation" style={{
              display: 'flex', gap: 6, marginTop: 4,
              opacity: 0, maxHeight: 0, overflow: 'hidden',
              transition: 'opacity 150ms ease, max-height 150ms ease',
              width: 'fit-content',
            }}
              onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => e.stopPropagation()}
            >
              <button className="wl-btn-done" onClick={() => handleSetStatus(item.id, 'done')} style={{
                background: 'rgba(34, 197, 94, 0.08)', border: 'none', borderRadius: 4,
                color: 'var(--success)', fontSize: 11, fontWeight: 500,
                padding: '3px 10px', cursor: 'pointer', transition: 'background 150ms ease',
              }}>{i18nT('apps.mochi.watchPanel.complete')}</button>
              <button className="wl-btn-cancel" onClick={() => handleSetStatus(item.id, 'cancelled')} style={{
                background: 'rgba(255, 107, 107, 0.08)', border: 'none', borderRadius: 4,
                color: 'var(--danger)', fontSize: 11, fontWeight: 500,
                padding: '3px 10px', cursor: 'pointer', transition: 'background 150ms ease',
              }}>{i18nT('apps.mochi.mochiPage.stop_watching')}</button>
            </div>
          )}
          {isTerminal && item.kind !== 'reminder' && item.kind !== 'meeting' && (
            <div className="wl-actions" role="presentation" style={{
              display: 'flex', gap: 6, marginTop: 4,
              opacity: 0, maxHeight: 0, overflow: 'hidden',
              transition: 'opacity 150ms ease, max-height 150ms ease',
              width: 'fit-content',
            }}
              onClick={(e) => e.stopPropagation()}
              onKeyDown={(e) => e.stopPropagation()}
            >
              <button className="wl-btn-reopen" onClick={() => handleSetStatus(item.id, 'watching')} style={{
                background: 'rgba(var(--accent-rgb, 100, 149, 237), 0.08)', border: 'none', borderRadius: 4,
                color: 'var(--accent)', fontSize: 11, fontWeight: 500,
                padding: '3px 10px', cursor: 'pointer', transition: 'background 150ms ease',
              }}>{i18nT('apps.mochi.watchPanel.reopen')}</button>
            </div>
          )}
        </div>
      )
    }

    return (
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 0', minHeight: 0 }}>
        {loading && (
          <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 11, padding: 24, opacity: 0.6 }}>{i18nT('apps.mochi.gallery.loading')}</div>
        )}

        {!loading && items.length === 0 && (
          <div style={{
            textAlign: 'center', color: 'var(--text-muted)', padding: '48px 20px',
            display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10,
          }}>
            <span style={{ opacity: 0.3, lineHeight: 1, color: 'var(--text-muted)' }}><Eye size={28} /></span>
            <span style={{ fontSize: 12, lineHeight: 1.6, opacity: 0.5 }}>
              {i18nT('apps.mochi.watchPanel.empty')}
            </span>
            <span style={{ fontSize: 11, opacity: 0.35, lineHeight: 1.5 }}>
              {i18nT('apps.mochi.watchPanel.empty_hint', { name: petName || DEFAULT_PET_NAME })}
            </span>
          </div>
        )}

        {!loading && activeItems.map(renderItem)}

        {/* Recently completed (< 24h) — shown inline after active */}
        {!loading && showCompleted && recentTerminal.length > 0 && (
          <>
            <div style={{
              fontSize: 10, color: 'var(--text-muted)', opacity: 0.4, fontWeight: 500,
              padding: '8px 12px 4px', textTransform: 'uppercase', letterSpacing: 0.5,
            }}>
              {i18nT('apps.mochi.watchPanel.recent_completed')}
            </div>
            {recentTerminal.map(renderItem)}
          </>
        )}

        {/* Older completed (>= 24h) — separate section at bottom */}
        {!loading && showCompleted && olderTerminal.length > 0 && (
          <>
            <div style={{
              fontSize: 10, color: 'var(--text-muted)', opacity: 0.4, fontWeight: 500,
              padding: '8px 12px 4px', textTransform: 'uppercase', letterSpacing: 0.5,
            }}>
              {i18nT('apps.mochi.watchPanel.older_completed')}
            </div>
            {olderTerminal.map(renderItem)}
          </>
        )}

        {!loading && terminalItems.length > 0 && (
          <button onClick={() => setShowCompleted(prev => !prev)} style={{
            display: 'block', margin: '8px auto 4px', background: 'none', border: 'none',
            color: 'var(--text-muted)', fontSize: 10, cursor: 'pointer',
          }}>
            {showCompleted
              ? i18nT('apps.mochi.watchPanel.hide_completed')
              : i18nT('apps.mochi.watchPanel.show_completed', { count: String(terminalItems.length) })}
          </button>
        )}

        {!loading && showCompleted && terminalItems.length > 0 && (
          <button onClick={() => setClearState('confirm')} style={{
            display: 'block', margin: '2px auto 8px', background: 'none', border: 'none',
            color: 'var(--danger)', fontSize: 10, cursor: 'pointer',
          }}>
            {i18nT('apps.mochi.watchPanel.clear_completed')}
          </button>
        )}
      </div>
    )
  }

  // ── Detail mode ─────────────────────────────────────────────────────────

  const renderDetailMode = () => {
    if (!selectedItem) return null

    const currentNotes = editDraft?.notes !== undefined ? editDraft.notes : (selectedItem.notes || '')
    const currentPriority = editDraft?.priority !== undefined ? editDraft.priority : selectedItem.priority
    const currentInterval = editDraft?.checkIntervalMins !== undefined
      ? editDraft.checkIntervalMins : selectedItem.checkIntervalMins

    const inputBg = 'rgba(255,255,255,0.04)'
    const divider = <div style={{ height: 1, background: 'rgba(255,255,255,0.1)', margin: '6px 0' }} />

    return (
      <div style={{ flex: 1, overflowY: 'auto', padding: '10px 14px', minHeight: 0 }}>
        {/* Back + actions row */}
        <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
          <button onClick={backToList} style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            fontSize: 12, cursor: 'pointer', padding: 0,
            display: 'flex', alignItems: 'center', gap: 4, opacity: 0.6,
          }}>
            <ArrowLeft size={13} /> {i18nT('apps.mochi.watchPanel.back')}
          </button>
          {!TERMINAL_STATUSES.includes(selectedItem.status) && (
            <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
              <button className="wl-btn-done" onClick={() => {
                backToList()
                setTimeout(() => handleSetStatus(selectedItem.id, 'done'), 50)
              }} style={{
                background: 'rgba(34, 197, 94, 0.08)', border: 'none', borderRadius: 4,
                color: 'var(--success)', fontSize: 11, fontWeight: 500,
                padding: '3px 10px', cursor: 'pointer', transition: 'background 150ms ease',
              }}>{i18nT('apps.mochi.watchPanel.complete')}</button>
              <button className="wl-btn-cancel" onClick={() => {
                backToList()
                setTimeout(() => handleSetStatus(selectedItem.id, 'cancelled'), 50)
              }} style={{
                background: 'rgba(255, 107, 107, 0.08)', border: 'none', borderRadius: 4,
                color: 'var(--danger)', fontSize: 11, fontWeight: 500,
                padding: '3px 10px', cursor: 'pointer', transition: 'background 150ms ease',
              }}>{i18nT('apps.mochi.mochiPage.stop_watching')}</button>
            </div>
          )}
          {TERMINAL_STATUSES.includes(selectedItem.status) && selectedItem.kind !== 'reminder' && selectedItem.kind !== 'meeting' && (
            <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
              <button className="wl-btn-reopen" onClick={() => {
                backToList()
                setTimeout(() => handleSetStatus(selectedItem.id, 'watching'), 50)
              }} style={{
                background: 'rgba(var(--accent-rgb, 100, 149, 237), 0.08)', border: 'none', borderRadius: 4,
                color: 'var(--accent)', fontSize: 11, fontWeight: 500,
                padding: '3px 10px', cursor: 'pointer', transition: 'background 150ms ease',
              }}>{i18nT('apps.mochi.watchPanel.reopen')}</button>
            </div>
          )}
        </div>

        {/* Label */}
        <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text)', lineHeight: 1.3, marginBottom: 6, letterSpacing: -0.3 }}>
          {selectedItem.label}
        </div>

        {/* Meta row: kind + status + priority — subtle, inline */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 12, flexWrap: 'wrap' }}>
          {[watchKindLabel(selectedItem.kind), watchStatusLabel(selectedItem.status), watchPriorityLabel(selectedItem.priority)].map((text, i) => (
            <span key={i} style={{
              padding: '2px 8px', borderRadius: 4, fontSize: 11, fontWeight: 500,
              background: 'rgba(255,255,255,0.06)', color: 'var(--text-muted)',
            }}>{text}</span>
          ))}
        </div>

        {/* Next check / Trigger time */}
        {!TERMINAL_STATUSES.includes(selectedItem.status) && (() => {
          const isTimeTrigger = selectedItem.kind === 'reminder' || selectedItem.kind === 'meeting'
          const timeField = isTimeTrigger ? selectedItem.triggerAt : selectedItem.nextCheckAfter
          if (!timeField) return null
          const cd = formatCountdown(timeField, now)
          const targetDate = new Date(timeField)
          const timeStr = fmtDateFields(targetDate, { hour: '2-digit', minute: '2-digit' })
          const isToday = targetDate.toDateString() === now.toDateString()
          const dateStr = isToday ? '' : ` · ${fmtDateFields(targetDate, { month: 'short', day: 'numeric' })}`
          const isOverdue = cd.kind === 'overdue'
          const label = isTimeTrigger
            ? i18nT('apps.mochi.watchPanel.field.trigger_at')
            : i18nT('apps.mochi.watchPanel.field.next_check')
          return (
            <div style={{
              padding: '10px 12px', borderRadius: 8, marginBottom: 12,
              background: isOverdue ? 'rgba(255, 107, 107, 0.06)' : 'rgba(255,255,255,0.03)',
              borderLeft: `3px solid ${isOverdue ? 'var(--danger)' : 'var(--accent)'}`,
            }}>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', opacity: 0.5, marginBottom: 3 }}>
                {label}
              </div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                <span style={{ fontSize: 14, fontWeight: 600, color: isOverdue ? 'var(--danger)' : 'var(--accent)', letterSpacing: -0.3 }}>
                  {i18nT(COUNTDOWN_KEY[cd.kind], cd.params)}
                </span>
                <span style={{ fontSize: 12, color: 'var(--text-muted)', opacity: 0.6 }}>
                  {timeStr}{dateStr}
                </span>
              </div>
            </div>
          )
        })()}

        {divider}

        {/* Notes */}
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4, opacity: 0.5, fontWeight: 500 }}>
          {i18nT('apps.mochi.watchPanel.field.notes')}
        </div>
        <textarea
          value={currentNotes}
          onChange={(e) => updateDraft('notes', e.target.value)}
          placeholder="..."
          style={{
            width: '100%', minHeight: 48, maxHeight: 200,
            fieldSizing: 'content' as any,
            background: inputBg,
            border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6,
            padding: '8px 10px', color: 'var(--text)', fontSize: 12,
            outline: 'none', resize: 'vertical', fontFamily: 'inherit',
            lineHeight: 1.5, marginBottom: 10,
          }}
        />

        {/* Priority + Interval side by side (interval hidden for reminder/meeting) */}
        <div style={{ display: 'flex', gap: 10, marginBottom: 10 }}>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4, opacity: 0.5, fontWeight: 500 }}>
              {i18nT('apps.mochi.watchPanel.field.priority')}
            </div>
            <select
              value={currentPriority}
              onChange={(e) => updateDraft('priority', e.target.value as WatchPriority)}
              style={{
                width: '100%', background: inputBg,
                border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6,
                padding: '6px 8px', color: 'var(--text)', fontSize: 12,
                outline: 'none', cursor: 'pointer',
              }}
            >
              <option value="high">{i18nT('apps.mochi.watchPanel.priority.high')}</option>
              <option value="normal">{i18nT('apps.mochi.watchPanel.priority.normal')}</option>
              <option value="low">{i18nT('apps.mochi.watchPanel.priority.low')}</option>
            </select>
          </div>
          {selectedItem.kind !== 'reminder' && selectedItem.kind !== 'meeting' && (
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 4, opacity: 0.5, fontWeight: 500 }}>
              {i18nT('apps.mochi.watchPanel.field.interval')}
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              <input
                type="number"
                min={intervalUnit === 'min' ? MIN_CHECK_INTERVAL_MINS : 1}
                value={
                  intervalUnit === 'day' ? Math.round(currentInterval / 1440) || 1
                  : intervalUnit === 'hr' ? Math.round(currentInterval / 60) || 1
                  : currentInterval
                }
                onChange={(e) => {
                  const v = Math.max(1, parseInt(e.target.value) || 1)
                  const mins = intervalUnit === 'day' ? v * 1440 : intervalUnit === 'hr' ? v * 60 : Math.max(MIN_CHECK_INTERVAL_MINS, v)
                  updateDraft('checkIntervalMins', mins)
                }}
                style={{
                  width: 52, background: inputBg,
                  border: '1px solid rgba(255,255,255,0.08)', borderRadius: 4,
                  padding: '5px 4px', color: 'var(--text)', fontSize: 12,
                  outline: 'none', textAlign: 'center',
                }}
              />
              <div style={{ display: 'flex', borderRadius: 4, overflow: 'hidden', border: '1px solid rgba(255,255,255,0.08)' }}>
                {(['min', 'hr', 'day'] as const).map(unit => (
                  <button key={unit} onClick={() => setIntervalUnit(unit)} style={{
                    background: intervalUnit === unit ? 'var(--accent)' : 'transparent',
                    color: intervalUnit === unit ? 'var(--accent-text)' : 'var(--text-muted)',
                    border: 'none', fontSize: 10, fontWeight: 600,
                    padding: '4px 7px', cursor: 'pointer',
                    transition: 'background 180ms ease, color 180ms ease',
                  }}>{unit}</button>
                ))}
              </div>
            </div>
          </div>
          )}
        </div>

        {/* Save */}
        <button onClick={handleSave} disabled={!hasDraftChanges} style={{
          width: '100%', padding: '7px 0', borderRadius: 6,
          border: 'none', fontSize: 12, fontWeight: 600,
          cursor: hasDraftChanges ? 'pointer' : 'default',
          background: hasDraftChanges ? 'var(--accent)' : 'rgba(255,255,255,0.05)',
          color: hasDraftChanges ? 'var(--accent-text)' : 'var(--text-muted)',
          opacity: hasDraftChanges ? 1 : 0.35,
          transition: 'all 0.15s',
        }}>{i18nT('apps.mochi.watchPanel.save')}</button>

        {divider}

        {/* Info — clean key-value rows (adapted per kind) */}
        {(() => {
          const isTimeTrigger = selectedItem.kind === 'reminder' || selectedItem.kind === 'meeting'
          const rows: [string, string][] = [
            [i18nT('apps.mochi.watchPanel.field.target'), selectedItem.target],
          ]
          if (!isTimeTrigger) {
            rows.push(
              [i18nT('apps.mochi.watchPanel.field.last_result'), selectedItem.lastResult || '—'],
              [i18nT('apps.mochi.watchPanel.field.last_checked'), selectedItem.lastChecked ? formatRelativeTime(selectedItem.lastChecked, now) : '—'],
              [i18nT('apps.mochi.watchPanel.field.check_count'), String(selectedItem.checkCount)],
            )
          }
          return rows.map(([label, value], i) => (
            <div key={i} style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
              padding: '6px 0', gap: 12,
              borderBottom: i < rows.length - 1 ? '1px solid rgba(255,255,255,0.08)' : 'none',
            }}>
              <span style={{ fontSize: 12, color: 'var(--text-muted)', opacity: 0.6, flexShrink: 0 }}>{label}</span>
              <span
                title={i === 0 ? value : undefined}
                style={{
                  fontSize: 12, color: 'var(--text)', fontWeight: 500, textAlign: 'right',
                  wordBreak: 'break-word', minWidth: 0,
                }}
              >{value}</span>
            </div>
          ))
        })()}

        {/* History */}
        {selectedItem.history && selectedItem.history.length > 0 && (() => {
          const changedEntries = selectedItem.history.filter(e => e.changed)
          const allEntries = selectedItem.history
          const hasHidden = allEntries.length > changedEntries.length
          const displayEntries = (showAllHistory ? allEntries : changedEntries).slice().reverse()

          return (
            <>
              {divider}
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{ fontSize: 11, color: 'var(--text-muted)', opacity: 0.5, fontWeight: 500 }}>
                  {i18nT('apps.mochi.watchPanel.field.history')}
                </span>
                {hasHidden && (
                  <button onClick={() => setShowAllHistory(prev => !prev)} style={{
                    background: 'none', border: 'none', color: 'var(--accent)',
                    fontSize: 10, cursor: 'pointer', padding: 0, opacity: 0.7,
                  }}>
                    {showAllHistory
                      ? i18nT('apps.mochi.watchPanel.history.show_changes')
                      : i18nT('apps.mochi.watchPanel.history.show_all', { count: String(allEntries.length) })}
                  </button>
                )}
              </div>
              {displayEntries.length === 0 && (
                <div style={{ fontSize: 11, color: 'var(--text-muted)', opacity: 0.4, padding: '4px 0' }}>
                  {i18nT('apps.mochi.watchPanel.history.no_changes')}
                </div>
              )}
              <div className="wl-history-timeline" style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                {displayEntries.map((entry, idx) => (
                  <div key={idx} className={`wl-history-entry${entry.changed ? ' wl-changed' : ''}`} style={{
                    padding: '6px 10px', borderRadius: 6, fontSize: 12,
                    background: entry.changed ? 'rgba(255,255,255,0.07)' : 'rgba(255,255,255,0.03)',
                    color: 'var(--text)', lineHeight: 1.5,
                  }}>
                    <span style={{ color: 'var(--text-muted)', opacity: 0.5, marginRight: 8, fontSize: 11 }}>
                      {formatRelativeTime(entry.checkedAt, now)}
                    </span>
                    {entry.result}
                  </div>
                ))}
              </div>
            </>
          )
        })()}
      </div>
    )
  }

  // ── Shell ──────────────────────────────────────────────────────────────

  // Panel visibility is controlled by the parent (ChatApp) via `visible` prop.
  // BrowserWindow width is adjusted instantly via IPC setBounds.
  // Panel content uses opacity + transform animation for smooth entrance.
  // No CSS width transition — it fights with setBounds.

  if (!visible) return null

  return (
    <div className="wl-panel-root" style={{
      width: WATCHLIST_PANEL_WIDTH, flexShrink: 0, height: '100vh',
      display: 'flex', flexDirection: 'column',
      borderLeft: '1px solid var(--border)',
      background: 'var(--bg)', overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        padding: '8px 14px', display: 'flex', alignItems: 'center', gap: 6,
        borderBottom: '1px solid var(--border)', fontSize: 12,
        // The rail sits inside the chat panel's body, so it takes the body
        // background. --header-bg is the title bar's colour and read as a
        // second title bar glued to the side.
        background: 'var(--bg)', flexShrink: 0,
        whiteSpace: 'nowrap',
      }}>
        <span style={{ fontWeight: 600, color: 'var(--text)', fontSize: 12 }}>
          {i18nT('apps.mochi.watchPanel.title')}
        </span>
        {items.filter(i => !TERMINAL_STATUSES.includes(i.status)).length > 0 && (
          <span style={{
            fontSize: 9, fontWeight: 600, color: 'var(--accent-text)',
            background: 'var(--accent)', borderRadius: 8,
            padding: '0 5px', lineHeight: '16px', minWidth: 16, textAlign: 'center',
          }}>
            {items.filter(i => !TERMINAL_STATUSES.includes(i.status)).length}
          </span>
        )}
        <button onClick={onClose} style={{
          marginLeft: 'auto', background: 'none', border: 'none',
          color: 'var(--text-muted)', fontSize: 13, cursor: 'pointer',
          padding: '0 2px', opacity: 0.6, transition: 'opacity 0.1s',
        }} aria-label={i18nT('apps.mochi.watchPanel.close')}
          onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.opacity = '1' }}
          onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.opacity = '0.6' }}
        ><X size={13} /></button>
      </div>

      {/* Content — single container, animation via class */}
      <div
        className={viewAnim === 'to-detail' ? 'wl-to-detail' : viewAnim === 'to-list' ? 'wl-to-list' : ''}
        style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}
      >
        {selectedItemId ? renderDetailMode() : renderListMode()}
      </div>

      {/* CSS hover + animations + focus */}
      <style>{`
        .wl-panel-root { animation: wl-panel-enter 0.2s ease-out both; }
        [role="button"] { transition: background 150ms ease; }
        [role="button"]:hover { background: rgba(255,255,255,0.03) }
        [role="button"]:hover .wl-actions { opacity: 1 !important; max-height: 32px !important; }
        /* Keyboard twin: without this, Tab reaches a zero-height invisible action
           button and Enter fires it blind. Reveal on focus-within too. */
        [role="button"]:focus-within .wl-actions { opacity: 1 !important; max-height: 32px !important; }
        .wl-panel-root ::-webkit-scrollbar { display: none; }
        .wl-panel-root { scrollbar-width: none; }
        .wl-btn-done:hover { background: rgba(34, 197, 94, 0.2) !important; }
        .wl-btn-cancel:hover { background: rgba(255, 107, 107, 0.2) !important; }
        .wl-btn-reopen:hover { background: rgba(var(--accent-rgb, 100, 149, 237), 0.2) !important; }
        @keyframes wl-panel-enter { from { opacity: 0; transform: translateX(-8px); } to { opacity: 1; transform: translateX(0); } }
        @keyframes wl-slide-in { from { opacity: 0; transform: translateX(-16px); } to { opacity: 1; transform: translateX(0); } }
        @keyframes wl-slide-out { from { opacity: 0; transform: translateX(16px); } to { opacity: 1; transform: translateX(0); } }
        .wl-to-detail { animation: wl-slide-in 0.22s cubic-bezier(0.25, 0.46, 0.45, 0.94) both; }
        .wl-to-list { animation: wl-slide-out 0.22s cubic-bezier(0.25, 0.46, 0.45, 0.94) both; }
        .wl-panel-root textarea:focus,
        .wl-panel-root select:focus,
        .wl-panel-root input:focus {
          border-color: var(--accent) !important;
          box-shadow: 0 0 0 2px rgba(var(--accent-rgb, 100, 149, 237), 0.15);
        }
        .wl-history-timeline {
          position: relative;
          padding-left: 12px;
        }
        .wl-history-timeline::before {
          content: '';
          position: absolute;
          left: 3px;
          top: 4px;
          bottom: 4px;
          width: 1px;
          background: rgba(255,255,255,0.08);
        }
        .wl-history-entry {
          position: relative;
        }
        .wl-history-entry::before {
          content: '';
          position: absolute;
          left: -12px;
          top: 10px;
          width: 5px;
          height: 5px;
          border-radius: 50%;
          background: rgba(255,255,255,0.15);
        }
        .wl-history-entry.wl-changed::before {
          background: var(--accent);
        }
      `}</style>

      {/* Clear-completed confirmation — innermost layer, so Escape lands here. */}
      {(clearState !== 'idle') && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={i18nT('apps.mochi.watchPanel.clear_confirm_title')}
          style={{
            position: 'fixed', inset: 0, zIndex: 1000,
            background: 'rgba(0,0,0,0.5)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <div style={{
            background: 'var(--bg-elevated)', borderRadius: 10, padding: '16px 20px',
            width: 240, border: '1px solid var(--border)', boxShadow: '0 8px 24px var(--shadow)',
          }}>
            <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)', marginBottom: 8 }}>
              {i18nT('apps.mochi.watchPanel.clear_confirm_title')}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 16, lineHeight: 1.4 }}>
              {clearState === 'failed'
                ? i18nT('apps.mochi.watchPanel.clear_failed')
                /* States the COUNT: "clear all completed" gives no sense of how
                   much is about to go, and this delete has no undo. */
                : i18nT('apps.mochi.watchPanel.clear_confirm_desc', {
                  count: String(terminalItems.length),
                })}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={() => setClearState('idle')} style={{
                background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6,
                padding: '5px 12px', color: 'var(--text)', fontSize: 12, cursor: 'pointer',
              }}>
                {i18nT(clearState === 'failed'
                  ? 'apps.mochi.watchPanel.clear_dismiss'
                  : 'apps.mochi.watchPanel.clear_cancel')}
              </button>
              {clearState !== 'failed' && (
                <button
                  disabled={clearState === 'working'}
                  onClick={async () => {
                    setClearState('working')
                    const ok = await api?.clearCompletedWatchItems?.()
                    if (ok === false) {
                      // Leave the items ON SCREEN: they still exist server-side,
                      // and the bridge's refresh will confirm that.
                      setClearState('failed')
                      return
                    }
                    setItems(prev => prev.filter(i => !TERMINAL_STATUSES.includes(i.status)))
                    setShowCompleted(false)
                    setClearState('idle')
                  }}
                  style={{
                    background: 'var(--danger)', border: 'none', borderRadius: 6,
                    padding: '5px 12px', color: '#fff', fontSize: 12, fontWeight: 600,
                    cursor: clearState === 'working' ? 'wait' : 'pointer',
                    opacity: clearState === 'working' ? 0.6 : 1,
                  }}
                >
                  {clearState === 'working'
                    ? i18nT('apps.mochi.watchPanel.clear_working')
                    : i18nT('apps.mochi.watchPanel.clear_confirm')}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
