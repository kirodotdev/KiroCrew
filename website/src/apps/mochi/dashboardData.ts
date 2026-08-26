/**
 * Pure shaping helpers for the Mochi dashboard page.
 *
 * Extracted from the original dashboard's inline logic (the render body did all
 * of this in place) for one reason: the plan-task type switch below is the kind
 * of code that fails INVISIBLY — a missed task type renders a blank or a raw
 * `[object Object]` row instead of throwing, so only a test that enumerates the
 * types can hold it.
 */
import { TERMINAL_STATUSES } from './api'
import type { ActivityEntry, MochiPlan, PlanTask, WatchItem } from './api'
import { fmtDate, fmtDateFields } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/**
 * Render any value as a string.
 *
 * The original wrapped every field in this before rendering because plan and
 * watch payloads are agent-authored: a nested object reaching JSX throws React
 * error #31 and takes the whole page down. The queue is a structural type over
 * parsed JSON (see queue_file.py), so the guarantee is still only "valid JSON",
 * and the wrapper is still load-bearing.
 */
export function str(v: unknown): string {
  if (v == null) return '—'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  return JSON.stringify(v)
}

/**
 * Title-case one word for a StatCard.
 *
 * The Status card already did this inline (`state.charAt(0).toUpperCase() + …`);
 * Mood renders the same kind of lowercase enum value and read as raw data
 * beside it.
 */
export function capitalize(v: string): string {
  return v === '' ? v : v.charAt(0).toUpperCase() + v.slice(1)
}

function hhmm(iso: string | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return fmtDateFields(d, { hour: '2-digit', minute: '2-digit' })
}

export interface TimelineRow {
  time: string
  action: string
  done: boolean
}

/** Describe one queued task in a single line. Mirrors the original's switch. */
function describeTask(task: PlanTask): string {
  const type = str(task.type ?? '')
  const action = (task.action ?? {}) as Record<string, unknown>
  const isObj = typeof task.action === 'object' && task.action !== null

  if (type === 'notify' || type === 'reminder') {
    return str(action.message ?? action.title ?? (isObj ? type : task.action))
  }
  if (type === 'mood') {
    return i18nT('apps.mochi.mochiPage.mood_change', {
      value: str(action.value ?? (isObj ? '?' : task.action)),
    })
  }
  if (type === 'move') {
    if (action.behavior !== undefined) return str(action.behavior).replace(/_/g, ' ')
    if (action.x !== undefined) return 'wander'
    return str(type)
  }
  if (type === 'watch' || type === 'check') {
    return str(action.label ?? (isObj ? type : task.action))
  }
  return str(action.message ?? action.label ?? type ?? '?')
}

/**
 * The planner's queue as a timeline.
 *
 * Only tasks carrying `execute_after` appear — an untimed task has no place on a
 * time-ordered list, and the original filtered the same way.
 */
export function formatPlanTasks(plan: MochiPlan | undefined): TimelineRow[] {
  const tasks = Array.isArray(plan?.tasks) ? plan!.tasks : []
  return tasks
    .filter((t) => Boolean(t?.execute_after))
    .map((t) => ({
      time: hhmm(t.execute_after),
      action: describeTask(t),
      done: Boolean(t.done),
    }))
}

/** Entry types that are bookkeeping rather than something the user did. */
const NOISE = new Set(['session_restore'])

export interface ActivityRow {
  time: string
  type: string
  content: string
}

/**
 * Activity entries as display rows, newest first.
 *
 * The narrative is prepended as a synthetic `plan` row so the card leads with
 * what Mochi believes it is currently doing — the original did this because the
 * narrative is the single most current line available and would otherwise only
 * appear buried in the Plan card.
 */
export function formatActivity(
  entries: ActivityEntry[] | undefined,
  narrative?: string
): ActivityRow[] {
  const rows = (entries ?? [])
    .filter((e) => !NOISE.has(e.type))
    // Descending by ISO timestamp. Byte order IS chronological order for ISO
    // 8601 strings, and the value is machine-formatted, so no collator applies.
    .sort((a, b) => ((a.ts || '') < (b.ts || '') ? 1 : (a.ts || '') > (b.ts || '') ? -1 : 0))
    .map((e) => ({
      time: hhmm(e.ts),
      type: str(e.type),
      content: str(e.content).slice(0, 150),
    }))
  if (narrative && narrative !== '—' && narrative !== 'Active') {
    rows.unshift({ time: 'now', type: 'plan', content: narrative })
  }
  return rows
}

export interface WatchRowView {
  id: string
  type: string
  label: string
  status: string
  notes: string
  trigger: string
  created: string
  checks: string
  nextCheck: string
  /** Cancelled items stay listed so the cancel is recoverable — see below. */
  cancelled: boolean
}

/**
 * Watch items as the expandable rows the original's Watchlist card renders.
 *
 * The dashboard is a "currently watching" surface, so items that reached a natural
 * end (done, failed, expired) are dropped here — the panel window still lists them.
 *
 * CANCELLED is deliberately NOT dropped. Cancelling is one unconfirmed click on a
 * row, and dropping the item made that click unrecoverable for anyone without the
 * desktop panel: no undo, no reopen, no feedback that anything happened. Keeping the
 * row (marked, with Reopen) is what makes the action reversible on this surface.
 */
export function formatWatchItems(items: WatchItem[] | undefined): WatchRowView[] {
  return (items ?? [])
    .filter((w) => w.status === 'cancelled' || !TERMINAL_STATUSES.includes(w.status))
    .map((w, i) => ({
    id: str(w.id || `wl-${i}`),
    type: str(w.kind),
    label: str(w.label || w.id),
    status: str(w.status),
    notes: str(w.notes || ''),
    trigger: str(w.triggerCondition || ''),
    created: w.createdAt ? fmtDate(w.createdAt) : '',
    checks: w.checkCount != null ? `${w.checkCount} checks` : '',
    nextCheck: w.nextCheckAfter
      ? fmtDateFields(w.nextCheckAfter, {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        })
      : '',
    cancelled: str(w.status) === 'cancelled',
  }))
}

/** Badge tone for a watch status. Terminal-good vs failed vs in-flight. */
export function statusTone(status: string): 'ok' | 'err' | 'warn' {
  if (['ok', 'done', 'merged', 'succeeded', 'watching'].includes(status)) return 'ok'
  if (['failed', 'error'].includes(status)) return 'err'
  return 'warn'
}
