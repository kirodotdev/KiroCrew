/**
 * WarmBriefCard — the catch-up a crewmate posts when the owner returns to an
 * active thread (`meta.kind === 'warm_task_brief_result'`).
 *
 * The gateway validates the model's JSON into `meta.brief` (`asked`, `now`,
 * `needs_you[]`, `next[]`) and this card draws it in a FIXED layout as the
 * BODY of the ordinary assistant bubble, so the row keeps its timestamp and
 * row actions (Copy, Reply in thread) and Copy takes the row's plain-text
 * `content`. Only an assistant row is ever a brief: the kind is a key in
 * `meta`, which a user row can carry too, and drawing that as the crewmate's
 * card would put words in the crewmate's mouth. Every
 * field is untrusted model output grounded in an untrusted snapshot, so each
 * one is drawn as a plain React text node: never markdown, never HTML, never a
 * link or a button. A row without a valid `meta.brief` (an older prose brief)
 * is not claimed here and keeps its ordinary bubble.
 */
import type { ReactNode } from 'react'
import { i18nT } from '../../i18n/t'
import type { ChatMessage } from '../../types'

export interface WarmBrief {
  asked: string
  now: string
  needsYou: string[]
  next: string[]
}

// The gateway caps every field already; these are the renderer's own bound so
// a hand-edited or foreign row can never stretch the card.
const MAX_FIELD = 240
const MAX_ITEMS = 3

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim().slice(0, MAX_FIELD) : ''
}

function items(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map(text).filter(Boolean).slice(0, MAX_ITEMS)
}

/** The row's validated brief, or null when this row is not a structured brief. */
export function warmBriefOf(m: ChatMessage): WarmBrief | null {
  if (m.role !== 'assistant') return null
  const meta = m.meta as { kind?: unknown; brief?: unknown } | undefined
  if (meta?.kind !== 'warm_task_brief_result') return null
  const raw = meta.brief
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const b = raw as Record<string, unknown>
  const now = text(b.now)
  if (!now) return null
  return { asked: text(b.asked), now, needsYou: items(b.needs_you), next: items(b.next) }
}

/** The text the card puts on screen, one text node per line, in DOM order, so
 *  chat search counts exactly the matches the highlighter can find. Labels
 *  follow the current UI language, as the card's do. */
export function warmBriefVisibleText(brief: WarmBrief): string {
  const lines: string[] = []
  if (brief.asked) lines.push(`${i18nT('components.warmBriefCard.asked')}: ${brief.asked}`)
  lines.push(brief.now)
  if (brief.needsYou.length) lines.push(i18nT('components.warmBriefCard.needs_you'), ...brief.needsYou)
  if (brief.next.length) lines.push(i18nT('components.warmBriefCard.next'), ...brief.next)
  return lines.join('\n')
}

/** The card as the assistant bubble's body, or undefined for any other row. */
export function warmBriefBodyOf(m: ChatMessage): ReactNode | undefined {
  const brief = warmBriefOf(m)
  return brief ? <WarmBriefCard brief={brief} /> : undefined
}

const SECTION_LABEL = 'text-[11px] leading-4 font-semibold mb-0.5'

export default function WarmBriefCard({ brief }: { brief: WarmBrief }) {
  return (
    <div
      className="min-w-0 flex flex-col gap-2"
      data-testid="warm-brief-card"
      role="group"
      aria-label={i18nT('components.warmBriefCard.label')}
    >
      {brief.asked && (
        <div className="text-[12px] leading-4 text-muted" data-testid="warm-brief-asked">
          {/* One text node, the same string search counts (`warmBriefVisibleText`):
              the highlighter matches within a single node. */}
          {`${i18nT('components.warmBriefCard.asked')}: ${brief.asked}`}
        </div>
      )}
      <div className="font-semibold text-text-strong" data-testid="warm-brief-now">{brief.now}</div>
      {brief.needsYou.length > 0 && (
        <div className="rounded-lg bg-warn-subtle text-text px-2.5 py-1.5 forced-colors:border" data-testid="warm-brief-needs-you">
          <div className={`${SECTION_LABEL} text-warn-text`}>{i18nT('components.warmBriefCard.needs_you')}</div>
          <ul className="m-0 pl-4 list-disc">
            {brief.needsYou.map((item, i) => <li key={i}>{item}</li>)}
          </ul>
        </div>
      )}
      {brief.next.length > 0 && (
        <div data-testid="warm-brief-next">
          <div className={`${SECTION_LABEL} text-muted`}>{i18nT('components.warmBriefCard.next')}</div>
          <ol className="m-0 pl-5 list-decimal">
            {brief.next.map((item, i) => <li key={i}>{item}</li>)}
          </ol>
        </div>
      )}
    </div>
  )
}
