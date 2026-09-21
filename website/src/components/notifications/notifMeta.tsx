import { ClipboardList, Anchor, Heart, Bot, Lock, GitBranch, Bell, Clock, BookOpen } from 'lucide-react'
import type { ReactNode } from 'react'

import { i18nT } from '../../i18n/t'
// Aliased: this module exports its own `fmtTime`/`fmtFull` wrappers that add the
// unknown-date fallback on top of these.
import { fmtTime as fmtClockTime, fmtDateTime, fmtDateFields, fmtRelative as fmtRelativeLocalized } from '../../i18n/format'

/**
 * Shared notification metadata + helpers, so the full page and the topbar bell
 * popover render notifications through the exact same code (one source of truth
 * for kinds, formatting, and date grouping).
 *
 * There is deliberately NO per-kind filter here. The feed used to carry a row of
 * nine toggle chips plus a persisted selection, which cost real complexity for
 * no reach: the list is short, free-text search already narrows it, and the
 * page's stat cards already break down volume by kind. Worse, the selection was
 * stored as an explicit list while the feed treated "every known kind selected"
 * as the special include-unknown-kinds state, so ADDING a kind silently turned a
 * stored full set into a partial one and hid the new kind for every existing
 * install. Removing the filter makes that failure mode impossible by
 * construction rather than managing it with a storage migration.
 */

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

/** Per-kind badge treatment. `label` is a getter, not a plain string: resolving
 *  it at module load would freeze every badge to the boot language and leave it
 *  stale after a language switch.
 *
 *  Three keys read fuller than their kind — `kind_cron_job`, `kind_webhook` and
 *  `kind_task_runner` back `cron`/`hook`/`task` — because a badge stands alone
 *  as a noun ('Cron Job', not 'Cron'). Don't rename them to match the kind for
 *  symmetry: these are the only labels these kinds have. */
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

/** Markdown → one-line plain-text excerpt: images keep their alt text, links
 *  their label; fence language tags, heading / emphasis / blockquote markers and
 *  list bullets are dropped. Shared by notification previews and the
 *  transcript turn minimap. */
export function stripMd(text: string): string {
  return text
    .replace(/```[\w-]*/g, ' ')
    .replace(/^\s{0,3}(?:[-+]|\d+\.)\s+/gm, '')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[*_~`#>]+/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/** macOS Notification Center-style relative timestamp ("now", "35m ago", "2h ago").
 *
 * Delegated to the locale-aware seam so relative times render in the app
 * language for every locale, with the "yesterday" literal from CLDR.
 *
 * Minute granularity is preserved deliberately — a notification feed that
 * counted seconds would rewrite every row on every tick. Anything under a
 * minute is collapsed to the locale's "now" rather than "45s ago". Shared by
 * the bell popover's mac cards and the in-app banner so the same note never
 * shows two different ages. */
export function fmtRelativeMinute(ts: string): string {
  const at = parseTs(ts)
  const now = Date.now()
  if (now - at.getTime() < 60_000) return fmtRelativeLocalized(now, { now })
  return fmtRelativeLocalized(at, { now })
}

/** The mac-variant floating card material, split so the bell popover's rows
 *  and the in-app banner share the blur and hairline border while each picks
 *  its own tint and shadow (the banner floats over arbitrary page content and
 *  needs a denser tint and a deeper shadow than a row inside the sheet's
 *  scrim). Every consumer must also carry `notif-material`, the index.css hook
 *  that solidifies these surfaces where backdrop-filter is unsupported. */
export const MAC_CARD_BLUR_CLASS = 'backdrop-blur-2xl backdrop-saturate-150'
export const MAC_CARD_BORDER_CLASS = 'border border-[color-mix(in_srgb,var(--border)_55%,transparent)]'
/** Popover rows: 72% card tint, the theme's medium elevation (inside the
 *  sheet's own scrim). Shadows are theme tokens (`--shadow-md` / `--shadow-lg`
 *  in index.css), which is what keeps them legible on both a light and a dark
 *  palette without a literal alpha here. */
export const MAC_CARD_TINT_CLASS = 'bg-[color-mix(in_srgb,var(--card)_72%,transparent)]'
export const MAC_CARD_SHADOW_CLASS = 'shadow-md'
/** Banner cards: 88% card tint, the theme's large elevation (floating over
 *  arbitrary page content). */
export const BANNER_CARD_TINT_CLASS = 'bg-[color-mix(in_srgb,var(--card)_88%,transparent)]'
export const BANNER_CARD_SHADOW_CLASS = 'shadow-lg'

/** macOS NC action buttons: quiet translucent capsules, text-only, with any
 *  semantic tint on the LABEL (never a solid coloured fill). The `bg-[…]`
 *  token leads because the i18n lint recognises an arbitrary-value class
 *  cluster by its FIRST bracketed token carrying a comma or underscore. */
export const MAC_ACTION_BTN_CLASS = 'bg-[color-mix(in_srgb,var(--bg-hover)_80%,transparent)] px-3 py-1 rounded-lg text-[12px] font-medium cursor-pointer font-body whitespace-nowrap transition-colors backdrop-blur border border-[color-mix(in_srgb,var(--border)_45%,transparent)] hover:bg-bg-hover'
