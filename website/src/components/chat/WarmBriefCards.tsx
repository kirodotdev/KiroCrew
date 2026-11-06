/**
 * WarmBriefCards — a version 3 warm welcome (`meta.brief.version === 3`): one
 * card per pull request or task, built by the gateway from the facts it
 * checked (RFC section 3.10).
 *
 * Every word on a card but the model's one sentence and its ask is the
 * gateway's, already in the brief's language: the status label, the check
 * line, titles and tags. Tags marked `bad` (a conflict, failed checks,
 * requested changes, a closed worker chat, a waiting approval) draw in the
 * danger tone. Every string is a plain text node. An ask's answers are the
 * subject's own source options, drawn as text: each with its consequence and
 * the recommended one starred. The reply's chip bar below the row stays the
 * one-click path. The PR pill links to the URL the gateway built from its own read,
 * re-checked here. At 320px the cards stack in one column; wider, two.
 */
import { i18nT } from '../../i18n/t'
import './warmBriefCards.css'

export interface WarmBriefTag { text: string; bad: boolean }

export interface WarmBriefCardV3 {
  title: string
  pr: { label: string; url: string; checked: boolean } | null
  tags: WarmBriefTag[]
  said: { text: string; by: string } | null
  replaced: string
  ask: {
    what: string
    why: string
    options: { label: string; consequence: string }[]
    recommended: string
    recommendedWhy: string
  } | null
}

export interface WarmBriefV3 {
  statusLabel: string
  check: string[]
  cards: WarmBriefCardV3[]
  /** "+N more" in the brief's language, or ''. */
  more: string
}

const MAX_FIELD = 240
const MAX_CARDS = 8
const MAX_TAGS = 12
const MAX_CHECK = 8
const MAX_OPTIONS = 6
const PR_URL_RE = /^https:\/\/github\.com\/[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}\/pull\/\d{1,9}$/
const PR_LABEL_RE = /^#\d{1,9}$/

const text = (v: unknown) => (typeof v === 'string' ? v.trim().slice(0, MAX_FIELD) : '')
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {})
const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

function cardOf(raw: unknown): WarmBriefCardV3 | null {
  const c = obj(raw)
  const title = text(c.title)
  if (!title) return null
  const p = obj(c.pr)
  const pr = PR_URL_RE.test(String(p.url ?? '')) && PR_LABEL_RE.test(String(p.label ?? ''))
    ? { label: p.label as string, url: p.url as string, checked: p.checked === true }
    : null
  const tags = list(c.tags).map(obj).map(t => ({ text: text(t.text), bad: t.bad === true })).filter(t => t.text).slice(0, MAX_TAGS)
  const s = obj(c.said)
  const said = text(s.text) ? { text: text(s.text), by: text(s.by) } : null
  const a = obj(c.ask)
  const options = list(a.options).map(obj).map(o => ({ label: text(o.label), consequence: text(o.consequence) })).filter(o => o.label).slice(0, MAX_OPTIONS)
  const recommended = options.some(o => o.label === text(a.recommended)) ? text(a.recommended) : ''
  const ask = text(a.what) || options.length
    ? { what: text(a.what), why: text(a.why), options, recommended, recommendedWhy: recommended ? text(a.recommended_why) : '' }
    : null
  return { title, pr, tags, said, replaced: text(c.replaced), ask }
}

/** The version 3 parts of a stored brief, or null for an older brief. */
export function warmBriefV3Of(b: Record<string, unknown>): WarmBriefV3 | null {
  if (b.version !== 3) return null
  return {
    statusLabel: text(b.status_label),
    check: list(b.check).map(text).filter(Boolean).slice(0, MAX_CHECK),
    cards: list(b.cards).map(cardOf).filter((c): c is WarmBriefCardV3 => c !== null).slice(0, MAX_CARDS),
    more: text(b.more),
  }
}

// A full-width colon carries its own space.
const saidText = (s: NonNullable<WarmBriefCardV3['said']>) => (s.by ? `${s.by}${s.by.endsWith('：') ? '' : ' '}${s.text}` : s.text)
const optionText = (o: { label: string; recommended: boolean }) => (o.recommended ? `★ ${o.label}` : o.label)

/** The cards' text in DOM order, for chat search. */
export function warmBriefCardsText(v3: WarmBriefV3): string[] {
  const lines: string[] = []
  if (v3.check.length) lines.push(v3.check.join(' · '))
  for (const card of v3.cards) {
    lines.push([card.pr?.label, card.title].filter(Boolean).join(' '), ...card.tags.map(t => t.text))
    if (card.said) lines.push(saidText(card.said))
    if (card.replaced) lines.push(card.replaced)
    if (card.ask) {
      if (card.ask.what) lines.push(card.ask.what)
      if (card.ask.why) lines.push(card.ask.why)
      lines.push(...card.ask.options.map(o => answerText(o, o.label === card.ask?.recommended)))
      if (card.ask.recommendedWhy) lines.push(card.ask.recommendedWhy)
    }
  }
  if (v3.more) lines.push(v3.more)
  return lines
}

const answerText = (o: { label: string; consequence: string }, recommended: boolean) =>
  `${optionText({ label: o.label, recommended })}${o.consequence ? `: ${o.consequence}` : ''}`

/** The answers as text: each option, what it does, and the pick starred. */
function Answers({ ask }: { ask: NonNullable<WarmBriefCardV3['ask']> }) {
  return (
    <ul className="wb3-said m-0 pl-4 list-disc" data-testid="warm-brief-v3-option-text">
      {ask.options.map((o, i) => (
        <li key={i} data-recommended={o.label === ask.recommended || undefined}>{answerText(o, o.label === ask.recommended)}</li>
      ))}
    </ul>
  )
}

function Card({ card }: { card: WarmBriefCardV3 }) {
  const waits = card.ask !== null || card.tags.some(t => t.bad)
  return (
    <li className={`wb3-card${waits ? ' wb3-wait' : ''}`} data-testid="warm-brief-v3-card">
      <div className="wb3-top">
        {card.pr && (
          card.pr.checked
            ? <a className="wb3-pill" href={card.pr.url} target="_blank" rel="noopener noreferrer" data-testid="warm-brief-v3-pill">
                <span className={`wb3-dot${card.tags.some(t => t.bad) ? ' wb3-dot-bad' : ''}`} aria-hidden="true" />{card.pr.label}
              </a>
            : <span className="wb3-pill wb3-unread" data-testid="warm-brief-v3-pill">{card.pr.label}</span>
        )}
        <span className="wb3-title">{card.title}</span>
      </div>
      {card.tags.length > 0 && (
        <ul className="wb3-tags" aria-label={i18nT('components.warmBriefCard.tags')}>
          {card.tags.map((t, i) => (
            <li key={i} className={`wb3-tag${t.bad ? ' wb3-bad' : ''}`} data-bad={t.bad || undefined} data-testid="warm-brief-v3-tag">{t.text}</li>
          ))}
        </ul>
      )}
      {card.said && <div className="wb3-said" data-testid="warm-brief-v3-said">{saidText(card.said)}</div>}
      {card.replaced && <div className="wb3-said" data-testid="warm-brief-v3-replaced">{card.replaced}</div>}
      {card.ask && (
        <div className="wb3-ask" data-testid="warm-brief-v3-ask">
          {card.ask.what && <div className="wb3-what">→ {card.ask.what}</div>}
          {card.ask.why && <div className="wb3-said">{card.ask.why}</div>}
          {card.ask.options.length > 0 && <Answers ask={card.ask} />}
          {card.ask.recommendedWhy && <div className="wb3-said">{card.ask.recommendedWhy}</div>}
        </div>
      )}
    </li>
  )
}

export default function WarmBriefCards({ v3 }: { v3: WarmBriefV3 }) {
  return (
    <>
      {v3.check.length > 0 && (
        <div className="wb3-check" data-testid="warm-brief-v3-check">
          {v3.check.map((part, i) => <span key={i}>{part}</span>)}
        </div>
      )}
      {v3.cards.length > 0 && (
        <div className="wb3-wrap">
          <ul className="wb3-cards" data-testid="warm-brief-v3-cards" aria-label={i18nT('components.warmBriefCard.label')}>
            {v3.cards.map((card, i) => <Card key={i} card={card} />)}
          </ul>
        </div>
      )}
      {v3.more && <div className="text-[12px] leading-4 text-muted" data-testid="warm-brief-v3-more">{v3.more}</div>}
    </>
  )
}
