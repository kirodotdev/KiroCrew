/**
 * WarmBriefCard — the catch-up a crewmate posts when the owner returns to an
 * active thread (`meta.kind === 'warm_task_brief_result'`).
 *
 * The gateway validates the model's JSON into `meta.brief` and this card draws
 * it: status and headline, then `WarmBriefWaiting` (the work items waiting on
 * the owner as cards, fixed code fed by gateway data), then the model's
 * free-form `body`. The body is markdown drawn by the same `MarkdownRenderer`
 * an ordinary assistant reply uses, so a mermaid diagram, a table or an
 * `<mcwidget>` the model chose renders as it would in chat, except that the
 * widget's `WidgetFrame` takes the `welcome` profile: page scripts and clicks,
 * no network, no composer pre-fill. A brief stored before that may carry the
 * model's `widget` HTML in its own field, drawn last. Briefs in the older shapes still draw their
 * fields (`asked`, `remaining`, `done`, `next`, `extra`), and one with `now`
 * draws it as the headline with no status chip. The card is the BODY of the ordinary assistant bubble,
 * so the row keeps its timestamp and row actions (Copy, Reply in thread) and
 * Copy takes the row's plain-text `content`. Only an assistant row is ever a
 * brief: the kind is a key in `meta`, which a user row can carry too, and
 * drawing that as the crewmate's card would put words in the crewmate's mouth.
 * Every field is untrusted model output grounded in an untrusted snapshot, so
 * each text field but `body` is drawn as a plain React text node: never
 * markdown, never HTML, never a link or a button. HTML only ever goes into the
 * same sandboxed `WidgetFrame` a chat `<mcwidget>` uses. A row without a valid `meta.brief` (an older prose
 * brief) is not claimed here and keeps its ordinary bubble.
 */
import type { ReactNode } from 'react'
import { i18nT } from '../../i18n/t'
import type { ChatMessage } from '../../types'
import MarkdownRenderer, { LinkOverrideCtx, LinkUnfurlCtx, type LinkOverride, type LinkUnfurl } from '../MarkdownRenderer'
import ErrorNotice from '../ErrorNotice'
import WidgetFrame from '../WidgetFrame'
import { WidgetFrameProfileContext } from '../widgetFrameProfile'
import WarmBriefWaiting, { SHOWN, type WarmBriefCardItem } from './WarmBriefWaiting'

export type WarmBriefStatus = 'on_track' | 'needs_you' | 'blocked' | 'done'

export interface WarmBrief {
  /** Null on an older brief, which had no status. */
  status: WarmBriefStatus | null
  headline: string
  asked: string
  remaining: string
  done: string[]
  needsYou: string[]
  next: string[]
  extra: { title: string; items: string[] }[]
  /** Free-form markdown; may hold a mermaid block, a table or an `<mcwidget>`. */
  body: string
  /** Work items waiting on the owner, read by the gateway from the ledger. */
  waiting: WarmBriefCardItem[]
  /** How many wait in all; above `waiting.length` only past the gateway's bound. */
  waitingTotal: number
  /** HTML an older brief carried in its own field, or ''. New briefs have none. */
  widget: string
  /** The gateway's link row: each pull request it read live, labelled `owner/repo#N`. */
  links: { url: string; label: string }[]
  /** Structured asks: why, each option's consequence, the recommendation.
   *  Option labels are the gateway-checked source labels. */
  asks: WarmBriefAsk[]
  /** What the gateway itself read for this brief; body lines cite them as `{fN}`. */
  checked: WarmBriefFact[]
  /** The model's typed lines; each line's marks come from its `facts` field. */
  lines: WarmBriefLine[]
}

/** One typed line: a plain sentence and the checked facts it states. */
export interface WarmBriefLine {
  text: string
  /** Fact ids (`f1`, ...); the gateway kept only ids its fact table holds. */
  facts: string[]
}

export interface WarmBriefFact {
  /** `f1`, `f2`, ...: the id body lines cite. */
  id: string
  label: string
  /** What was read, as words in the UI language (`factWords`). */
  facts: string[]
  /** Where it was read, translated: GitHub or the task list. */
  source: string
  readAt: string
  /** The read failed: the row is "not checked since HH:MM", an error. */
  failed: boolean
}

export interface WarmBriefAsk {
  what: string
  why: string
  options: { label: string; consequence: string }[]
  /** One of the option labels, or ''. */
  recommended: string
  recommendedWhy: string
}

// The gateway caps every field already; these are the renderer's own bound so
// a hand-edited or foreign row can never stretch the card.
const MAX_FIELD = 240
const MAX_ITEMS = 4
// Matches the gateway's bound on listed waiting items.
const MAX_WAITING = 50
const MAX_DONE = 4
const MAX_EXTRA = 2
// Matches the gateway's cap on the body.
const MAX_BODY = 6000
// A widget over this is dropped whole: cut HTML would draw broken.
const MAX_WIDGET = 8000
const STATUSES: readonly WarmBriefStatus[] = ['on_track', 'needs_you', 'blocked', 'done']
// The gateway reads at most five PRs live, so its row holds at most five.
const MAX_LINKS = 5
const PR_URL_RE = /^https:\/\/github\.com\/([A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100})\/pull\/(\d{1,9})$/
// A widget draws in its own frame, out of the chat search's reach.
const MCWIDGET_RE = /<mcwidget\b[^>]*>[\s\S]*?<\/mcwidget>/gi

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim().slice(0, MAX_FIELD) : ''
}

function items(value: unknown, limit = MAX_ITEMS): string[] {
  if (!Array.isArray(value)) return []
  return value.map(v => text(v)).filter(Boolean).slice(0, limit)
}

/** The link row: well-formed GitHub PR URLs only, each labelled from its own URL. */
function linksOf(value: unknown): WarmBrief['links'] {
  if (!Array.isArray(value)) return []
  const links: WarmBrief['links'] = []
  for (const url of value) {
    const m = typeof url === 'string' ? PR_URL_RE.exec(url) : null
    if (m && !links.some(l => l.url === url)) links.push({ url: url as string, label: `${m[1]}#${m[2]}` })
  }
  return links.slice(0, MAX_LINKS)
}

function waitingOf(value: unknown): WarmBriefCardItem[] {
  if (!Array.isArray(value)) return []
  return value
    .map(raw => {
      const s = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
      return { title: text(s.title), ask: text(s.ask) }
    })
    .filter(w => w.title)
    .slice(0, MAX_WAITING)
}

const MAX_ASK_OPTIONS = 6
// Every id the gateway can mint (`f1`..`f200`, its `MAX_FACTS`), so each
// fact a line lists keeps its row; and the gateway's bound on typed lines.
const MAX_FACTS = 200
const MAX_LINES = 12
/** `f1` .. `f200` exactly, compared as text, the ids the gateway mints. */
function isFactId(id: unknown): id is string {
  const n = typeof id === 'string' && id.startsWith('f') ? Number(id.slice(1)) : NaN
  return Number.isInteger(n) && n >= 1 && n <= MAX_FACTS && id === `f${n}`
}

// The gateway's machine values, each with its word in the UI language; a
// value outside the list is shown as read, lower-cased.
const FACT_VALUE_KEYS = {
  open: 'components.warmBriefCard.value_open', closed: 'components.warmBriefCard.value_closed',
  merged: 'components.warmBriefCard.value_merged', draft: 'components.warmBriefCard.value_draft',
  accepted: 'components.warmBriefCard.value_accepted', rejected: 'components.warmBriefCard.value_rejected',
  abandoned: 'components.warmBriefCard.value_abandoned', progress: 'components.warmBriefCard.value_progress',
  question: 'components.warmBriefCard.value_question', blocked: 'components.warmBriefCard.value_blocked',
  done: 'components.warmBriefCard.value_done', APPROVED: 'components.warmBriefCard.value_approved',
  CHANGES_REQUESTED: 'components.warmBriefCard.value_changes_requested',
  REVIEW_REQUIRED: 'components.warmBriefCard.value_review_required',
  MERGEABLE: 'components.warmBriefCard.value_no_conflicts', CLEAN: 'components.warmBriefCard.value_can_merge',
  CONFLICTING: 'components.warmBriefCard.value_conflicts', DIRTY: 'components.warmBriefCard.value_conflicts',
} as const
const count = (v: unknown) => (Number.isInteger(v) && (v as number) >= 0 ? (v as number) : 0)

/** One fact's values as words in the UI language, in the card's order. */
export function factWords(values: Record<string, unknown>): string[] {
  const since = values.not_checked_since
  if (typeof since === 'string') {
    return [since === 'never' ? i18nT('components.warmBriefCard.not_checked_never') : i18nT('components.warmBriefCard.not_checked_since', { time: text(since) })]
  }
  const words = (['state', 'status', 'review', 'mergeable'] as const)
    .map(k => text(values[k]))
    .filter(v => v && v !== 'UNKNOWN')
    .map(v => (v in FACT_VALUE_KEYS ? i18nT(FACT_VALUE_KEYS[v as keyof typeof FACT_VALUE_KEYS]) : v.toLowerCase().split('_').join(' ')))
  const checks = values.checks
  if (typeof checks === 'string' && checks) words.push(i18nT('components.warmBriefCard.checks_not_read'))
  else if (checks && typeof checks === 'object') {
    const c = checks as Record<string, unknown>
    const parts = [i18nT('components.warmBriefCard.checks_counts', { passed: count(c.passed), failed: count(c.failed) })]
    if (count(c.pending)) parts.push(i18nT('components.warmBriefCard.checks_running', { n: count(c.pending) }))
    if (count(c.unknown)) parts.push(i18nT('components.warmBriefCard.checks_unknown', { n: count(c.unknown) }))
    words.push(parts.join(', '))
  }
  return words
}

function checkedOf(value: unknown): WarmBriefFact[] {
  if (!Array.isArray(value)) return []
  return value
    .map(raw => {
      const s = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
      const id = isFactId(s.id) ? s.id : ''
      const values = s.values && typeof s.values === 'object' ? (s.values as Record<string, unknown>) : {}
      const source = s.source === 'github' ? i18nT('components.warmBriefCard.source_github') : s.source === 'tasks' ? i18nT('components.warmBriefCard.source_tasks') : ''
      return { id, label: text(s.label), facts: factWords(values), source, readAt: text(s.read_at), failed: typeof values.not_checked_since === 'string' }
    })
    .filter(f => f.id && f.label)
    .slice(0, MAX_FACTS)
}

const SUPERSCRIPT = '⁰¹²³⁴⁵⁶⁷⁸⁹'
/** `f12` as the footnote mark `¹²` the body and the Checked row share. */
const mark = (digits: string) => [...digits].map(d => SUPERSCRIPT[Number(d)]).join('')
/** A typed line with its fact ids drawn as the Checked rows' marks. */
export const lineText = (l: WarmBriefLine) => `${l.text}${l.facts.map(id => mark(id.slice(1))).join(',')}`

function linesOf(value: unknown): WarmBriefLine[] {
  if (!Array.isArray(value)) return []
  return value
    .map(raw => {
      const s = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
      return { text: text(s.text), facts: (Array.isArray(s.facts) ? s.facts : []).filter(isFactId).slice(0, MAX_FACTS) }
    })
    .filter(l => l.text)
    .slice(0, MAX_LINES)
}

/** One checked fact as one line: `¹ owner/repo#N: merged · approved (GitHub 09:30)`. */
export function factText(f: WarmBriefFact): string {
  const values = f.facts.length ? `: ${f.facts.join(' · ')}` : ''
  const where = [f.source, f.readAt].filter(Boolean).join(' ')
  return `${mark(f.id.slice(1))} ${f.label}${values}${where ? ` (${where})` : ''}`
}

function asksOf(value: unknown): WarmBriefAsk[] {
  if (!Array.isArray(value)) return []
  return value
    .map(raw => {
      const s = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
      const options = (Array.isArray(s.options) ? s.options : [])
        .map(o => (o && typeof o === 'object' ? (o as Record<string, unknown>) : {}))
        .map(o => ({ label: text(o.label), consequence: text(o.consequence) }))
        .filter(o => o.label)
        .slice(0, MAX_ASK_OPTIONS)
      const recommended = options.some(o => o.label === text(s.recommended)) ? text(s.recommended) : ''
      return { what: text(s.what), why: text(s.why), options, recommended, recommendedWhy: recommended ? text(s.recommended_why) : '' }
    })
    .filter(a => a.what)
    .slice(0, MAX_ITEMS)
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
  const waiting = waitingOf(b.waiting)
  const total = b.waiting_total
  return {
    status: STATUSES.find(s => s === b.status) ?? null,
    headline,
    asked: text(b.asked),
    remaining: text(b.remaining),
    done: items(b.done, MAX_DONE),
    needsYou: items(b.needs_you),
    next: items(b.next),
    extra: extraOf(b.extra),
    // Code points, as the gateway counts: a UTF-16 cut could split a pair or drop a closing tag.
    body: typeof b.body === 'string' ? Array.from(b.body.trim()).slice(0, MAX_BODY).join('') : '',
    waiting,
    waitingTotal: Number.isInteger(total) && (total as number) > waiting.length ? (total as number) : waiting.length,
    widget: typeof b.widget === 'string' && b.widget.length <= MAX_WIDGET ? b.widget.trim() : '',
    links: linksOf(b.links),
    asks: asksOf(b.asks),
    checked: checkedOf(b.checked),
    lines: linesOf(b.lines),
  }
}

const whyText = (ask: WarmBriefAsk) => `${i18nT('components.warmBriefCard.why')}: ${ask.why}`
const optionText = (o: WarmBriefAsk['options'][number]) => (o.consequence ? `${o.label}: ${o.consequence}` : o.label)
const recommendedText = (ask: WarmBriefAsk) =>
  `${i18nT('components.warmBriefCard.recommended')}: ${ask.recommended}${ask.recommendedWhy ? ` (${ask.recommendedWhy})` : ''}`

/** One ask's lines, in DOM order: what, why, each option, the recommendation. */
function askLines(ask: WarmBriefAsk): string[] {
  return [ask.what, ...(ask.why ? [whyText(ask)] : []), ...ask.options.map(optionText), ...(ask.recommended ? [recommendedText(ask)] : [])]
}

function statusLabel(status: WarmBriefStatus): string {
  switch (status) {
    case 'on_track': return i18nT('components.warmBriefCard.status_on_track')
    case 'needs_you': return i18nT('components.warmBriefCard.status_needs_you')
    case 'blocked': return i18nT('components.warmBriefCard.status_blocked')
    case 'done': return i18nT('components.warmBriefCard.status_done')
  }
}
const remainingText = (brief: WarmBrief) => `${i18nT('components.warmBriefCard.remaining')}: ${brief.remaining}`
const askedText = (brief: WarmBrief) => `${i18nT('components.warmBriefCard.asked')}: ${brief.asked}`

/** The text the card puts on screen, in DOM order, so chat search reads what
 *  the owner sees. The body is its markdown minus any `<mcwidget>`, the same
 *  rule an ordinary assistant reply's search follows. Labels follow the
 *  current UI language, as the card's do. */
export function warmBriefVisibleText(brief: WarmBrief): string {
  const lines: string[] = []
  if (brief.status) lines.push(statusLabel(brief.status))
  lines.push(brief.headline)
  if (hasCards(brief)) {
    const cards = cardsOf(brief)
    for (const card of cards.slice(0, SHOWN)) lines.push(card.title, ...(card.ask ? [card.ask] : []))
    if (cards.length > SHOWN) {
      const folded = cards.length - SHOWN + brief.waitingTotal - brief.waiting.length
      lines.push(i18nT('components.warmBriefCard.more_waiting', { count: folded }))
    }
  } else if (brief.needsYou.length) {
    lines.push(i18nT('components.warmBriefCard.needs_you'), ...brief.needsYou)
  }
  if (brief.remaining) lines.push(remainingText(brief))
  for (const ask of brief.asks) lines.push(...askLines(ask))
  lines.push(...brief.lines.map(lineText))
  const body = brief.body.replace(MCWIDGET_RE, '').trim()
  if (body) lines.push(body)
  lines.push(...brief.links.map(l => l.label))
  const read = brief.checked.filter(f => !f.failed)
  if (read.length) lines.push(i18nT('components.warmBriefCard.checked'), ...read.map(factText))
  lines.push(...brief.checked.filter(f => f.failed).map(factText))
  if (brief.asked) lines.push(askedText(brief))
  if (brief.done.length) lines.push(i18nT('components.warmBriefCard.done'), ...brief.done)
  if (brief.next.length) lines.push(i18nT('components.warmBriefCard.next'), ...brief.next)
  for (const section of brief.extra) lines.push(section.title, ...section.items)
  return lines.join('\n')
}

/** The waiting cards: the gateway's waiting items, then the model's other asks. */
function cardsOf(brief: WarmBrief): WarmBriefCardItem[] {
  return [...brief.waiting, ...brief.needsYou.map(title => ({ title, ask: '' }))]
}

/** The structured asks. Every string is a plain text node; the recommended
 *  option is marked, never a button (the working buttons are the chip bar's). */
function Asks({ asks }: { asks: WarmBriefAsk[] }) {
  return (
    <ol className="m-0 pl-5 list-decimal flex flex-col gap-1.5 break-words" data-testid="warm-brief-ask-list" aria-label={i18nT('components.warmBriefCard.needs_you')}>
      {asks.map((ask, i) => (
        <li key={i} data-testid="warm-brief-ask">
          <div className="font-medium text-text-strong">{ask.what}</div>
          {ask.why && <div className="text-[12px] leading-4 text-muted">{whyText(ask)}</div>}
          {ask.options.length > 0 && (
            <ul className="m-0 pl-4 list-disc text-[12px] leading-4">
              {ask.options.map((o, j) => (
                <li key={j} data-recommended={o.label === ask.recommended || undefined}>{optionText(o)}</li>
              ))}
            </ul>
          )}
          {ask.recommended && <div className="text-[12px] leading-4 font-semibold" data-testid="warm-brief-recommended">{recommendedText(ask)}</div>}
        </li>
      ))}
    </ol>
  )
}

/** A brief with a status (every brief since the status chip) draws its asks
 *  as cards; an older `now` brief keeps its plain Needs you box. */
function hasCards(brief: WarmBrief): boolean {
  return brief.status !== null
}

/** The card as the assistant bubble's body, or undefined for any other row.
 *  *slotKey* is the same one the host gives an ordinary reply's renderer, so a
 *  body widget is the same chat widget, saved to the same session. */
export function warmBriefBodyOf(m: ChatMessage, slotKey?: string): ReactNode | undefined {
  const brief = warmBriefOf(m)
  return brief ? <WarmBriefCard brief={brief} messageTs={m.ts} slotKey={slotKey} /> : undefined
}

/** The link row never fetches a preview: its pills are built from the URL alone. */
const NO_UNFURL: LinkUnfurl = { enabled: false, live: false }

/** Every link in the body draws as its text: no anchor, no chip, no unfurl. */
const inertLink: LinkOverride = ({ children }) => <span data-testid="warm-brief-inert-link">{children}</span>

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

export default function WarmBriefCard({ brief, messageTs, slotKey }: { brief: WarmBrief; messageTs?: string; slotKey?: string }) {
  const cards = cardsOf(brief)
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
      {(cards.length > 0 || brief.remaining) && (
        <div data-testid="warm-brief-asks">
          {hasCards(brief) ? (
            cards.length > 0 && <WarmBriefWaiting cards={cards} unlisted={brief.waitingTotal - brief.waiting.length} />
          ) : brief.needsYou.length > 0 && (
            <div className="rounded-lg bg-warn-subtle text-text px-2.5 py-1.5 forced-colors:border" data-testid="warm-brief-needs-you">
              <div className={`${SECTION_LABEL} text-warn-text`}>{i18nT('components.warmBriefCard.needs_you')}</div>
              <ul className="m-0 pl-4 list-disc break-words">
                {brief.needsYou.map((item, i) => <li key={i}>{item}</li>)}
              </ul>
            </div>
          )}
          {brief.remaining && <div className="text-[12px] leading-4 mt-1 text-center break-words">{remainingText(brief)}</div>}
        </div>
      )}
      {brief.asks.length > 0 && <Asks asks={brief.asks} />}
      {brief.lines.length > 0 && (
        <ul className="m-0 pl-4 list-disc break-words" data-testid="warm-brief-lines">
          {brief.lines.map((line, i) => <li key={i}>{lineText(line)}</li>)}
        </ul>
      )}
      {brief.body && (
        <div className="min-w-0 break-words" data-testid="warm-brief-body">
          {/* The chat's own renderer, with the welcome frame profile: an
              <mcwidget> keeps page scripts and clicks but gets no network and
              cannot reach the composer (widgetFrameProfile.ts). */}
          {/* Links stay text: the body is untrusted, and links belong to the asks. */}
          <LinkOverrideCtx.Provider value={inertLink}>
            <WidgetFrameProfileContext.Provider value="welcome">
              <MarkdownRenderer content={brief.body} messageTs={messageTs} slotKey={slotKey} />
            </WidgetFrameProfileContext.Provider>
          </LinkOverrideCtx.Provider>
        </div>
      )}
      {brief.links.length > 0 && (
        <div className="min-w-0" data-testid="warm-brief-links">
          {/* The gateway's own row: PRs it read live, the URL from each read's
              repo. Bare URLs through the chat's renderer draw as its PR pills
              (provider mark + `owner/repo#N`); no preview is fetched. */}
          <LinkUnfurlCtx.Provider value={NO_UNFURL}>
            <MarkdownRenderer content={brief.links.map(link => link.url).join(' ')} messageTs={messageTs} slotKey={slotKey} />
          </LinkUnfurlCtx.Provider>
        </div>
      )}
      {brief.checked.some(f => !f.failed) && (
        <Section title={i18nT('components.warmBriefCard.checked')} list={brief.checked.filter(f => !f.failed).map(factText)} testId="warm-brief-checked" />
      )}
      {/* A failed read is an error (RFC 3.9.1 wording kept), drawn by the shared error surface. */}
      {brief.checked.filter(f => f.failed).map(f => (
        <ErrorNotice key={f.id} variant="inline" message={factText(f)} askAgent testId="warm-brief-not-checked" />
      ))}
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
          {/* Model-written too, so it takes the same welcome profile. */}
          <WidgetFrameProfileContext.Provider value="welcome">
            <WidgetFrame html={brief.widget} title={i18nT('components.warmBriefCard.label')} />
          </WidgetFrameProfileContext.Provider>
        </div>
      )}
    </div>
  )
}
