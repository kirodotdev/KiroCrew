import { ClipboardList, Anchor, Heart, Bot, Lock, GitBranch, Bell, Clock, BookOpen } from 'lucide-react'
import type { ReactNode } from 'react'

import { i18nT } from '../../i18n/t'
import { safeGetItem } from '../../utils/safeStorage'
// Aliased: this module exports its own `fmtTime`/`fmtFull` wrappers that add the
// unknown-date fallback on top of these.
import { fmtTime as fmtClockTime, fmtDateTime, fmtDateFields } from '../../i18n/format'

/**
 * Shared notification metadata + helpers, so the full page and the topbar bell
 * popover render notifications through the exact same code (one source of truth
 * for kinds, filters, formatting, and date grouping).
 */

export type Kind = 'cron' | 'hook' | 'heartbeat' | 'agent' | 'approval' | 'subagent' | 'taskrunner' | 'skills'
export type Category = 'all' | Kind

export const KIND_KEYS: Kind[] = ['cron', 'hook', 'heartbeat', 'agent', 'approval', 'subagent', 'taskrunner', 'skills']

/** The kind set that shipped under {@link LEGACY_KINDS_STORAGE_KEY}, frozen.
 *  Used ONLY by the one-time filter migration in {@link loadActiveKinds} to tell
 *  "the user had everything selected" apart from "the user chose these seven" --
 *  which are byte-identical in the v1 payload. Never extend it. */
const LEGACY_KIND_KEYS: Kind[] = ['cron', 'hook', 'heartbeat', 'agent', 'approval', 'subagent', 'taskrunner']

/**
 * Filter chips across the top of the feed.
 *
 * `label` is a GETTER, not a string: this table is evaluated once at module load,
 * so an `i18nT()` call in the initializer would freeze the boot language and never
 * re-resolve when the language switches. A getter moves the lookup to property-access
 * time, which happens during `NotificationFeed`'s render.
 *
 * `lib/effort.ts` solves the same problem with a key table plus an exported resolver;
 * a getter is used here because the two consumers that read `.label`
 * (`NotificationFeed.tsx`, `NotificationDetailPanel.tsx`) stay untouched, so the
 * property has to keep behaving like a `string`.
 *
 * The keys are inline literals at each `i18nT()` call — the only form
 * `scripts/check-i18n-keys.mjs` can resolve statically.
 */
export const CATEGORIES: { key: Category; label: string; icon: ReactNode }[] = [
  { key: 'all', get label() { return i18nT('components.notifications.notifMeta.kind_all') }, icon: <ClipboardList className="lucide-inline" /> },
  { key: 'cron', get label() { return i18nT('components.notifications.notifMeta.kind_cron') }, icon: <Clock className="lucide-inline" /> },
  { key: 'hook', get label() { return i18nT('components.notifications.notifMeta.kind_hooks') }, icon: <Anchor className="lucide-inline" /> },
  { key: 'heartbeat', get label() { return i18nT('components.notifications.notifMeta.kind_heartbeat') }, icon: <Heart className="lucide-inline" /> },
  { key: 'agent', get label() { return i18nT('components.notifications.notifMeta.kind_agent') }, icon: <Bot className="lucide-inline" /> },
  { key: 'approval', get label() { return i18nT('components.notifications.notifMeta.kind_approval') }, icon: <Lock className="lucide-inline" /> },
  { key: 'subagent', get label() { return i18nT('components.notifications.notifMeta.kind_subagent') }, icon: <GitBranch className="lucide-inline" /> },
  { key: 'taskrunner', get label() { return i18nT('components.notifications.notifMeta.kind_tasks') }, icon: <ClipboardList className="lucide-inline" /> },
  { key: 'skills', get label() { return i18nT('components.notifications.notifMeta.kind_skills') }, icon: <BookOpen className="lucide-inline" /> },
]

/** Current filter-selection storage key.
 *
 *  Versioned because the selection is stored as an EXPLICIT list of kinds while
 *  `NotificationFeed` treats "every known kind selected" as the special
 *  include-unknown-kinds state (`allActive`). Adding a kind therefore turns a
 *  stored full set into a 7-of-8 PARTIAL set, which flips filtering to strict
 *  and hides the new kind permanently -- for every existing install, with no
 *  chip that brings it back until the user happens to toggle something. The
 *  version bump makes that migration explicit instead of silent. */
export const KINDS_STORAGE_KEY = 'mc:notif:activeKinds:v2'
/** Pre-`skills` key. Read by {@link loadActiveKinds} and never written.
 *
 *  Deliberately NOT deleted after migrating: `loadActiveKinds` is a `useState`
 *  initializer, so removing it there would put a storage write in render, and
 *  the stale entry is inert once the versioned key exists (v2 always wins). It
 *  costs a few bytes per install and buys a render with no side effects. */
export const LEGACY_KINDS_STORAGE_KEY = 'mc:notif:activeKinds'

function parseKinds(raw: string | null): Kind[] | null {
  if (!raw) return null
  try {
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return null
    return arr.filter((k: unknown): k is Kind => typeof k === 'string' && (KIND_KEYS as string[]).includes(k))
  } catch {
    return null
  }
}

export function loadActiveKinds(): Set<Kind> {
  // `safeGetItem`, not a bare `localStorage.getItem`: reading storage THROWS
  // (SecurityError) when a browser policy or embedding context blocks it, and
  // this function is a `useState` initializer -- an uncaught throw there takes
  // the whole notification feed down, not just the filter selection. The
  // try/catch inside `parseKinds` covers only JSON.parse, which is a different
  // failure.
  const current = parseKinds(safeGetItem(KINDS_STORAGE_KEY))
  if (current) return new Set(current)
  // One-time migration off the unversioned key. A v1 set covering every legacy
  // kind meant "all" (the default, and the state the "All" chip produces), so
  // it migrates to all CURRENT kinds -- otherwise the user silently loses the
  // include-unknown-kinds behaviour they never opted out of. A genuine subset
  // carries over verbatim: it was a deliberate choice, and the kinds added
  // since were not available to deselect, so leaving them off is the honest
  // reading. Not written back here -- the feed's persist effect owns writes.
  const legacy = parseKinds(safeGetItem(LEGACY_KINDS_STORAGE_KEY))
  if (legacy) {
    const had = new Set(legacy)
    if (LEGACY_KIND_KEYS.every(k => had.has(k))) return new Set(KIND_KEYS)
    return had
  }
  return new Set(KIND_KEYS)
}

export function parseTs(ts: string | number): Date {
  // A numeric epoch (number, or an all-digits string) can arrive in any unit —
  // seconds, milliseconds, microseconds, or nanoseconds — depending on the
  // producer. Detect the unit by magnitude and normalize to milliseconds.
  //
  // Detecting the unit up front (rather than `new Date(ts)` with a
  // `new Date(parseFloat(ts) * 1000)` fallback) is required because a
  // millisecond epoch passed as a string is Invalid Date in V8, so the fallback
  // would treat it as seconds and render the year as ~58527. It also handles the
  // microsecond-as-number case.
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
  return fmtDateFields(d, { year: 'numeric', month: 'short' })
}

/** Per-kind badge treatment. `label` is a getter for the same reason as
 *  `CATEGORIES` above — module-load evaluation would freeze the boot language.
 *  The badge register is INDEPENDENT of the chip register: three kinds are
 *  deliberately worded differently here ('Cron Job' vs the 'Cron' chip,
 *  'Webhook' vs 'Hooks', 'Task Runner' vs 'Tasks'), so those get their own keys;
 *  the four that read identically share the chip's key rather than shipping a
 *  duplicate English string to ten locales. */
export const KIND_META: Record<string, { icon: ReactNode; color: string; label: string; borderColor: string }> = {
  cron:       { icon: <Clock className="lucide-inline" />, color: 'bg-accent/15 text-accent',  get label() { return i18nT('components.notifications.notifMeta.kind_cron_job') },     borderColor: 'border-l-accent' },
  hook:       { icon: <Anchor className="lucide-inline" />, color: 'bg-info/15 text-info',      get label() { return i18nT('components.notifications.notifMeta.kind_webhook') },      borderColor: 'border-l-info' },
  heartbeat:  { icon: <Heart className="lucide-inline" />, color: 'bg-ok/15 text-ok',          get label() { return i18nT('components.notifications.notifMeta.kind_heartbeat') },    borderColor: 'border-l-ok' },
  agent:      { icon: <Bot className="lucide-inline" />, color: 'bg-info/15 text-info',      get label() { return i18nT('components.notifications.notifMeta.kind_agent') },        borderColor: 'border-l-info' },
  approval:   { icon: <Lock className="lucide-inline" />, color: 'bg-warn/15 text-warn',      get label() { return i18nT('components.notifications.notifMeta.kind_approval') },     borderColor: 'border-l-warn' },
  subagent:   { icon: <GitBranch className="lucide-inline" />, color: 'bg-accent/15 text-accent',  get label() { return i18nT('components.notifications.notifMeta.kind_subagent') },     borderColor: 'border-l-accent' },
  taskrunner: { icon: <ClipboardList className="lucide-inline" />, color: 'bg-accent/15 text-accent',  get label() { return i18nT('components.notifications.notifMeta.kind_task_runner') }, borderColor: 'border-l-accent' },
  skills:     { icon: <BookOpen className="lucide-inline" />, color: 'bg-warn/15 text-warn',      get label() { return i18nT('components.notifications.notifMeta.kind_skills') },       borderColor: 'border-l-warn' },
}
export const DEFAULT_META = { icon: <Bell className="lucide-inline" />, color: 'bg-muted/15 text-muted', get label() { return i18nT('components.notifications.notifMeta.kind_notification') }, borderColor: 'border-l-muted' }

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
