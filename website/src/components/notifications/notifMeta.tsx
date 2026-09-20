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

/** Markdown → plain-text excerpt: images keep their alt text, links
 *  their label. Paired formatting delimiters are unwrapped; code contents and
 *  unpaired markers remain literal. Prose paragraphs are joined with a visible
 *  separator. Shared by notification previews and the transcript turn minimap. */
export function stripMd(text: string): string {
  // A body arrives from persisted rows and the API, so the type is a hope,
  // not a guarantee: a legacy or corrupted row can carry a truthy non-string.
  // Three consumers call this (OS banner, feed excerpt, minimap); guarding
  // here keeps a bad row from throwing past any of them.
  if (typeof text !== 'string') return ''
  // The separator idiom the detail panel already uses between a label and a
  // session title. Punctuation, not copy.
  const PARAGRAPH_SEPARATOR = ' · '
  // Alternate prose and literal code. The cursor only advances: an unmatched
  // fence owns the rest of the input instead of retrying at every backtick.
  const parts: string[] = []
  let start = 0
  let lineStart = 0
  let fenceLength = 0
  let inlineStart = -1
  let inlineLength = 0
  let cursor = 0
  // The final line ending belongs to the fence wrapper, not its code value.
  const fenceContent = (end: number) => text.slice(start,
    end > start && text[end - 1] === '\n' ? end - (text[end - 2] === '\r' ? 2 : 1) : end)
  while (cursor < text.length) {
    if (text[cursor] === '\n') {
      // A DELIBERATE deviation from CommonMark, which lets an inline span span
      // lines: here a newline ends an unclosed one. In a preview that matters,
      // because one stray backtick would otherwise pair with another paragraphs
      // away and hold everything between it as literal code, suppressing the
      // flattening for that whole region. Bounding an unmatched delimiter to its
      // own line costs only multi-line inline spans, which no producer writes.
      lineStart = ++cursor
      inlineStart = -1
      continue
    }
    if (text[cursor] !== '`') {
      cursor++
      continue
    }
    const runStart = cursor
    while (text[cursor] === '`') cursor++
    const runLength = cursor - runStart
    const atLinePrefix = runStart - lineStart <= 3
      && /^[ \t]*$/.test(text.slice(lineStart, runStart))
    if (fenceLength) {
      if (atLinePrefix && runLength >= fenceLength) {
        while (text[cursor] === ' ' || text[cursor] === '\t' || text[cursor] === '\r') cursor++
        if (cursor === text.length || text[cursor] === '\n') {
          parts.push(fenceContent(lineStart))
          start = cursor
          fenceLength = 0
        }
      }
    } else if (atLinePrefix && runLength >= 3) {
      parts.push(text.slice(start, lineStart))
      fenceLength = runLength
      // The whole info string is metadata, including spaces and punctuation.
      const newline = text.indexOf('\n', cursor)
      cursor = newline < 0 ? text.length : newline + 1
      start = lineStart = cursor
      inlineStart = -1
    } else if (inlineStart < 0) {
      inlineStart = runStart
      inlineLength = runLength
    } else if (runLength === inlineLength) {
      parts.push(text.slice(start, inlineStart), text.slice(inlineStart + inlineLength, runStart))
      start = cursor
      inlineStart = -1
    }
  }
  parts.push(fenceLength ? fenceContent(text.length) : text.slice(start))
  // Prose owns its whitespace and paragraph boundaries. Defer a prose space
  // until more content follows so trimming never reaches into literal code.
  const paragraphs: string[] = ['']
  let pendingSpace = false
  parts.forEach((part, index) => {
    if (index % 2) {
      if (part) {
        if (pendingSpace && paragraphs[paragraphs.length - 1]) paragraphs[paragraphs.length - 1] += ' '
        paragraphs[paragraphs.length - 1] += part
        pendingSpace = false
      }
      return
    }
    part
      // A code boundary inside a line is not a heading or list boundary.
      .replace(/^[ \t]{0,3}(?:[-+*]|\d+\.|#{1,6}|>)[ \t]+/gm,
        (marker, offset: number) => offset === 0 && index > 0 ? marker : '')
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/\*\*(?!\s)([^*\n]*?)(?<!\s)\*\*/g, '$1')
      .replace(/(?<!\w)__(?!\s)([^_\n]*?)(?<!\s)__(?!\w)/g, '$1')
      .replace(/(?<!\*)\*(?![\s*])([^*\n]*?)(?<!\s)\*(?!\*)/g, '$1')
      .replace(/(?<![\w_])_(?!\s)([^_\n]*?)(?<!\s)_(?![\w_])/g, '$1')
      .replace(/~~(?!\s)([^~\n]*?)(?<!\s)~~/g, '$1')
      .split(/\r?\n(?:[ \t]*\r?\n)+/)
      .forEach((paragraph, paragraphIndex) => {
        if (paragraphIndex) {
          paragraphs.push('')
          pendingSpace = false
        }
        const prose = paragraph.replace(/\s+/g, ' ')
        const content = prose.trim()
        if (content) {
          if (paragraphs[paragraphs.length - 1] && (pendingSpace || prose.startsWith(' '))) {
            paragraphs[paragraphs.length - 1] += ' '
          }
          paragraphs[paragraphs.length - 1] += content
          pendingSpace = prose.endsWith(' ')
        } else if (prose) {
          pendingSpace = true
        }
      })
  })
  // Empty paragraphs drop out, so the separator never leads, trails or doubles.
  return paragraphs
    .filter(Boolean)
    .join(PARAGRAPH_SEPARATOR)
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
