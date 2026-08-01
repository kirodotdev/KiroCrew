import { ClipboardList, Anchor, Heart, Bot, Lock, GitBranch, Bell, Clock } from 'lucide-react'
import type { ReactNode } from 'react'

import { i18nT } from '../../i18n/t'
// Aliased: this module exports its own `fmtTime`/`fmtFull` wrappers that add the
// unknown-date fallback on top of these.
import { fmtTime as fmtClockTime, fmtDateTime } from '../../i18n/format'

/**
 * Shared notification metadata + helpers. Extracted verbatim from the former
 * inline NotificationsPage so the full page and the topbar bell popover render
 * notifications through the exact same code (one source of truth for kinds,
 * filters, formatting, and date grouping).
 */

export type Kind = 'cron' | 'hook' | 'heartbeat' | 'agent' | 'approval' | 'subagent' | 'taskrunner'
export type Category = 'all' | Kind

export const KIND_KEYS: Kind[] = ['cron', 'hook', 'heartbeat', 'agent', 'approval', 'subagent', 'taskrunner']

export const CATEGORIES: { key: Category; label: string; icon: ReactNode }[] = [
  { key: 'all', label: 'All', icon: <ClipboardList className="lucide-inline" /> },
  { key: 'cron', label: 'Cron', icon: <Clock className="lucide-inline" /> },
  { key: 'hook', label: 'Hooks', icon: <Anchor className="lucide-inline" /> },
  { key: 'heartbeat', label: 'Heartbeat', icon: <Heart className="lucide-inline" /> },
  { key: 'agent', label: 'Agent', icon: <Bot className="lucide-inline" /> },
  { key: 'approval', label: 'Approval', icon: <Lock className="lucide-inline" /> },
  { key: 'subagent', label: 'Subagent', icon: <GitBranch className="lucide-inline" /> },
  { key: 'taskrunner', label: 'Tasks', icon: <ClipboardList className="lucide-inline" /> },
]

export const KINDS_STORAGE_KEY = 'mc:notif:activeKinds'

export function loadActiveKinds(): Set<Kind> {
  try {
    const raw = localStorage.getItem(KINDS_STORAGE_KEY)
    if (raw) {
      const arr = JSON.parse(raw)
      if (Array.isArray(arr)) {
        const valid = arr.filter((k: unknown): k is Kind => typeof k === 'string' && (KIND_KEYS as string[]).includes(k))
        return new Set(valid)
      }
    }
  } catch { /* fall through to default */ }
  return new Set(KIND_KEYS)
}

export function parseTs(ts: string | number): Date {
  // A numeric epoch (number, or an all-digits string) can arrive in any unit —
  // seconds, milliseconds, microseconds, or nanoseconds — depending on the
  // producer. Detect the unit by magnitude and normalize to milliseconds.
  //
  // The previous implementation did `new Date(ts)` then, only on failure,
  // `new Date(parseFloat(ts) * 1000)`. That broke for a millisecond epoch
  // passed as a string: `new Date("1784784205932")` is Invalid Date in V8, so
  // it fell through and multiplied ms by 1000 (treating it as seconds), which
  // rendered the year as ~58527. Detecting the unit up front fixes both the
  // ms-as-string and microsecond-as-number cases.
  const num =
    typeof ts === 'number'
      ? ts
      : /^\s*\d+(\.\d+)?\s*$/.test(ts)
        ? parseFloat(ts)
        : NaN
  let d: Date
  if (!isNaN(num)) {
    let ms: number
    if (num >= 1e17) ms = num / 1e6 // nanoseconds → ms
    else if (num >= 1e14) ms = num / 1e3 // microseconds → ms
    else if (num >= 1e11) ms = num // milliseconds (already)
    else ms = num * 1e3 // seconds → ms
    d = new Date(ms)
  } else {
    d = new Date(ts) // ISO 8601 / RFC date string
  }
  if (isNaN(d.getTime()) || d.getTime() < Date.UTC(2020, 0, 1)) return new Date(NaN)
  return d
}

export function dateGroup(d: Date): string {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today.getTime() - 86400000)
  const weekAgo = new Date(today.getTime() - 6 * 86400000)
  if (d >= today) return i18nT('components.notifications.notifMeta.today')
  if (d >= yesterday) return i18nT('components.notifications.notifMeta.yesterday')
  if (d >= weekAgo) return i18nT('components.notifications.notifMeta.this_week')
  return d.toLocaleDateString([], { year: 'numeric', month: 'short' })
}

export const KIND_META: Record<string, { icon: ReactNode; color: string; label: string; borderColor: string }> = {
  cron:       { icon: <Clock className="lucide-inline" />, color: 'bg-accent/15 text-accent',  label: 'Cron Job',   borderColor: 'border-l-accent' },
  hook:       { icon: <Anchor className="lucide-inline" />, color: 'bg-info/15 text-info',      label: 'Webhook',    borderColor: 'border-l-info' },
  heartbeat:  { icon: <Heart className="lucide-inline" />, color: 'bg-ok/15 text-ok',          label: 'Heartbeat',  borderColor: 'border-l-ok' },
  agent:      { icon: <Bot className="lucide-inline" />, color: 'bg-info/15 text-info',      label: 'Agent',      borderColor: 'border-l-info' },
  approval:   { icon: <Lock className="lucide-inline" />, color: 'bg-warn/15 text-warn',      label: 'Approval',   borderColor: 'border-l-warn' },
  subagent:   { icon: <GitBranch className="lucide-inline" />, color: 'bg-accent/15 text-accent',  label: 'Subagent',   borderColor: 'border-l-accent' },
  taskrunner: { icon: <ClipboardList className="lucide-inline" />, color: 'bg-accent/15 text-accent',  label: 'Task Runner', borderColor: 'border-l-accent' },
}
export const DEFAULT_META = { icon: <Bell className="lucide-inline" />, color: 'bg-muted/15 text-muted', label: 'Notification', borderColor: 'border-l-muted' }

/** RFC Phase 3 priority tiers -- visual treatment per level (mockup 3):
 *  critical pops with a danger edge + marker, passive dims, default is
 *  unchanged. Silenced (muted channel) is handled separately as a
 *  dashed-border ghost behind the "Show muted" filter. */
export const PRIORITIES = ['critical', 'default', 'passive'] as const
export type Priority = (typeof PRIORITIES)[number]

export function notePriority(n: { priority?: string }): Priority {
  return n.priority === 'critical' || n.priority === 'passive' ? n.priority : 'default'
}

/** RFC Phase 4 security: deep-links must be dashboard-internal routes only.
 *  Mirrors the backend validator in notifications/bus.py -- path-only, no
 *  protocol-relative ("//host"), no backslashes (WHATWG normalizes "\" to
 *  "/"), no tab/newline/CR tricks. Returns the url when safe, else null. */
export function safeInternalUrl(url: string | undefined): string | null {
  if (!url || !url.startsWith('/')) return null
  if (url.startsWith('//') || url.includes('\\') || /[\t\n\r]/.test(url)) return null
  return url
}

export function fmtTime(ts: string | number): string {
  const d = parseTs(ts)
  return isNaN(d.getTime()) ? i18nT('components.notifications.notifMeta.unknown_date') : fmtClockTime(d)
}

export function fmtFull(ts: string | number): string {
  const d = parseTs(ts)
  return isNaN(d.getTime()) ? i18nT('components.notifications.notifMeta.unknown_date') : fmtDateTime(d)
}

export function stripMd(text: string): string {
  return text.replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/[*_~`#>]+/g, '').replace(/\n+/g, ' ').trim()
}
