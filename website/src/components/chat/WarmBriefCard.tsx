/**
 * WarmBriefCard — the catch-up a crewmate posts when the owner returns to an
 * active thread (`meta.kind === 'warm_task_brief_result'`).
 *
 * The gateway validates the model's JSON into `meta.brief` and this card draws
 * it: status and headline, what needs the owner, progress (the gateway's own
 * `accepted of total` count, never the model's), the model's free-form `body`,
 * then an optional `widget`. Briefs in the older shapes still draw their
 * fields (`asked`, `remaining`, `done`, `next`, `extra`), and one with `now`
 * draws it as the headline with no status chip. The card is the BODY of the ordinary assistant bubble,
 * so the row keeps its timestamp and row actions (Copy, Reply in thread) and
 * Copy takes the row's plain-text `content`. Only an assistant row is ever a
 * brief: the kind is a key in `meta`, which a user row can carry too, and
 * drawing that as the crewmate's card would put words in the crewmate's mouth.
 * Every field is untrusted model output grounded in an untrusted snapshot, so
 * each text field is drawn as a plain React text node: never markdown, never
 * HTML, never a link or a button. The widget is HTML and goes only into the
 * same sandboxed `WidgetFrame` a chat `<mcwidget>` uses. A row without a valid `meta.brief` (an older prose
 * brief) is not claimed here and keeps its ordinary bubble.
 */
import type { ReactNode } from 'react'
import { i18nT } from '../../i18n/t'
import type { ChatMessage } from '../../types'
import WidgetFrame from '../WidgetFrame'

export type WarmBriefStatus = 'on_track' | 'needs_you' | 'blocked' | 'done'

export interface WarmBrief {
  /** Null on an older brief, which had no status. */
  status: WarmBriefStatus | null
  headline: string
  asked: string
  progress: { accepted: number; total: number } | null
  remaining: string
  done: string[]
  needsYou: string[]
  next: string[]
  extra: { title: string; items: string[] }[]
  /** Free-form plain text; line breaks are kept. */
  body: string
  /** HTML for one sandboxed widget, or ''. */
  widget: string
}

// The gateway caps every field already; these are the renderer's own bound so
// a hand-edited or foreign row can never stretch the card.
const MAX_FIELD = 240
const MAX_ITEMS = 3
const MAX_DONE = 4
const MAX_EXTRA = 2
const MAX_BODY = 1200
// A widget over this is dropped whole: cut HTML would draw broken.
const MAX_WIDGET = 8000
const STATUSES: readonly WarmBriefStatus[] = ['on_track', 'needs_you', 'blocked', 'done']

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim().slice(0, MAX_FIELD) : ''
}

function items(value: unknown, limit = MAX_ITEMS): string[] {
  if (!Array.isArray(value)) return []
  return value.map(v => text(v)).filter(Boolean).slice(0, limit)
}

function progressOf(value: unknown): WarmBrief['progress'] {
  if (!value || typeof value !== 'object') return null
  const { accepted, total } = value as Record<string, unknown>
  if (!Number.isInteger(accepted) || !Number.isInteger(total)) return null
  const a = accepted as number
  const t = total as number
  return t > 0 && a >= 0 && a <= t ? { accepted: a, total: t } : null
}

function extraOf(value: unknown): WarmBrief['extra'] {
  if (!Array.isArray(value)) return []
  return value
    .map(raw => {
      const s = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
      return { title: text(s.title), items: items(s.items) }
    })
    .filter(s => s.title && s.items.length)
    .slice(0, MAX_EXTRA)
}

/** The row's validated brief, or null when this row is not a structured brief. */
export function warmBriefOf(m: ChatMessage): WarmBrief | null {
  if (m.role !== 'assistant') return null
  const meta = m.meta as { kind?: unknown; brief?: unknown } | undefined
  if (meta?.kind !== 'warm_task_brief_result') return null
  const raw = meta.brief
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const b = raw as Record<string, unknown>
  const headline = text(b.headline) || text(b.now)
  if (!headline) return null
  return {
    status: STATUSES.find(s => s === b.status) ?? null,
    headline,
    asked: text(b.asked),
    progress: progressOf(b.progress),
    remaining: text(b.remaining),
    done: items(b.done, MAX_DONE),
    needsYou: items(b.needs_you),
    next: items(b.next),
    extra: extraOf(b.extra),
    body: typeof b.body === 'string' ? b.body.trim().slice(0, MAX_BODY) : '',
    widget: typeof b.widget === 'string' && b.widget.length <= MAX_WIDGET ? b.widget.trim() : '',
  }
}

function statusLabel(status: WarmBriefStatus): string {
  switch (status) {
    case 'on_track': return i18nT('components.warmBriefCard.status_on_track')
    case 'needs_you': return i18nT('components.warmBriefCard.status_needs_you')
    case 'blocked': return i18nT('components.warmBriefCard.status_blocked')
    case 'done': return i18nT('components.warmBriefCard.status_done')
  }
}
const countText = (p: NonNullable<WarmBrief['progress']>) =>
  i18nT('components.warmBriefCard.progress_count', { accepted: p.accepted, total: p.total })
const remainingText = (brief: WarmBrief) => `${i18nT('components.warmBriefCard.remaining')}: ${brief.remaining}`
const askedText = (brief: WarmBrief) => `${i18nT('components.warmBriefCard.asked')}: ${brief.asked}`

/** The text the card puts on screen, one text node per line, in DOM order, so
 *  chat search counts exactly the matches the highlighter can find. Labels
 *  follow the current UI language, as the card's do. */
export function warmBriefVisibleText(brief: WarmBrief): string {
  const lines: string[] = []
  if (brief.status) lines.push(statusLabel(brief.status))
  lines.push(brief.headline)
  if (brief.needsYou.length) lines.push(i18nT('components.warmBriefCard.needs_you'), ...brief.needsYou)
  if (brief.progress || brief.remaining) lines.push(i18nT('components.warmBriefCard.progress'))
  if (brief.progress) lines.push(countText(brief.progress))
  if (brief.remaining) lines.push(remainingText(brief))
  if (brief.body) lines.push(brief.body)
  if (brief.asked) lines.push(askedText(brief))
  if (brief.done.length) lines.push(i18nT('components.warmBriefCard.done'), ...brief.done)
  if (brief.next.length) lines.push(i18nT('components.warmBriefCard.next'), ...brief.next)
  for (const section of brief.extra) lines.push(section.title, ...section.items)
  return lines.join('\n')
}

/** The card as the assistant bubble's body, or undefined for any other row. */
export function warmBriefBodyOf(m: ChatMessage): ReactNode | undefined {
  const brief = warmBriefOf(m)
  return brief ? <WarmBriefCard brief={brief} /> : undefined
}

const SECTION_LABEL = 'text-[11px] leading-4 font-semibold mb-0.5'
const STATUS_CHIP: Record<WarmBriefStatus, string> = {
  on_track: 'bg-ok-subtle text-ok-text',
  needs_you: 'bg-warn-subtle text-warn-text',
  blocked: 'bg-danger-subtle text-danger-text',
  done: 'bg-ok-subtle text-ok-text',
}

function Section({ title, list, testId, ordered = false }: { title: string; list: string[]; testId: string; ordered?: boolean }) {
  const List = ordered ? 'ol' : 'ul'
  return (
    <div data-testid={testId}>
      <div className={`${SECTION_LABEL} text-muted`}>{title}</div>
      <List className={`m-0 ${ordered ? 'pl-5 list-decimal' : 'pl-4 list-disc'}`}>
        {list.map((item, i) => <li key={i}>{item}</li>)}
      </List>
    </div>
  )
}

export default function WarmBriefCard({ brief }: { brief: WarmBrief }) {
  const { progress } = brief
  return (
    <div
      className="min-w-0 flex flex-col gap-2"
      data-testid="warm-brief-card"
      role="group"
      aria-label={i18nT('components.warmBriefCard.label')}
    >
      <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-1">
        {brief.status && (
          <span
            className={`shrink-0 rounded-full px-2 text-[11px] leading-5 font-semibold forced-colors:border ${STATUS_CHIP[brief.status]}`}
            data-testid="warm-brief-status"
            data-status={brief.status}
          >
            {statusLabel(brief.status)}
          </span>
        )}
        <span className="min-w-0 font-semibold text-text-strong" data-testid="warm-brief-headline">{brief.headline}</span>
      </div>
      {brief.needsYou.length > 0 && (
        <div className="rounded-lg bg-warn-subtle text-text px-2.5 py-1.5 forced-colors:border" data-testid="warm-brief-needs-you">
          <div className={`${SECTION_LABEL} text-warn-text`}>{i18nT('components.warmBriefCard.needs_you')}</div>
          <ul className="m-0 pl-4 list-disc">
            {brief.needsYou.map((item, i) => <li key={i}>{item}</li>)}
          </ul>
        </div>
      )}
      {(progress || brief.remaining) && (
        <div data-testid="warm-brief-progress">
          <div className={`${SECTION_LABEL} text-muted`}>{i18nT('components.warmBriefCard.progress')}</div>
          {progress && (
            <div className="flex items-center gap-2">
              {/* Static width, no transition: the card never animates. */}
              <div
                className="h-1.5 flex-1 min-w-[48px] overflow-hidden rounded-full bg-border forced-colors:border"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={progress.total}
                aria-valuenow={progress.accepted}
                aria-label={i18nT('components.warmBriefCard.progress')}
              >
                <div className="h-full bg-accent" style={{ width: `${(progress.accepted / progress.total) * 100}%` }} />
              </div>
              <span className="shrink-0 text-[12px] leading-4 tabular-nums" data-testid="warm-brief-progress-count">{countText(progress)}</span>
            </div>
          )}
          {brief.remaining && <div className="text-[12px] leading-4 mt-0.5">{remainingText(brief)}</div>}
        </div>
      )}
      {brief.body && <div className="whitespace-pre-line break-words" data-testid="warm-brief-body">{brief.body}</div>}
      {brief.asked && (
        <div className="text-[12px] leading-4 text-muted" data-testid="warm-brief-asked">
          {/* One text node, the same string search counts (`warmBriefVisibleText`):
              the highlighter matches within a single node. */}
          {askedText(brief)}
        </div>
      )}
      {brief.done.length > 0 && <Section title={i18nT('components.warmBriefCard.done')} list={brief.done} testId="warm-brief-done" />}
      {brief.next.length > 0 && <Section title={i18nT('components.warmBriefCard.next')} list={brief.next} testId="warm-brief-next" ordered />}
      {brief.extra.map((section, i) => (
        <Section key={i} title={section.title} list={section.items} testId="warm-brief-extra" />
      ))}
      {brief.widget && (
        <div className="min-w-0" data-testid="warm-brief-widget">
          <WidgetFrame html={brief.widget} title={i18nT('components.warmBriefCard.label')} />
        </div>
      )}
    </div>
  )
}
