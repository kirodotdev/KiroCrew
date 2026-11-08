/**
 * WarmWelcomeCard — the warm welcome the gateway renders (`meta.brief.version === 4`,
 * RFC rfc-delayed-task-brief.md 3.10.4).
 *
 * Every word here is a string the gateway wrote from its own reads, in the welcome's
 * one language: the count line, each must-do card's tags and read time, the
 * conductor's own words (labelled with who said them and when), the changes since
 * the owner left and the Board footer. The model's only text is the optional
 * `framing` line and an optional fact-free mermaid `body`, both gated by the gateway.
 * So the card draws stored strings as plain React text nodes: never markdown, HTML,
 * a link or a button, except the Board link (a route this app owns, built from the
 * conductor key) and the PR pill (a GitHub URL the gateway built from its own read).
 * The mermaid body goes through the chat's renderer under the `welcome` frame
 * profile, links inert. Older briefs keep drawing through `WarmBriefCard`.
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { i18nT } from '../../i18n/t'
import type { ChatMessage } from '../../types'
import MarkdownRenderer, { LinkOverrideCtx, LinkUnfurlCtx, type LinkOverride, type LinkUnfurl } from '../MarkdownRenderer'
import { WidgetFrameProfileContext } from '../widgetFrameProfile'
import './warmWelcomeCard.css'

const MAX_TEXT = 400
const MAX_CARDS = 3
const MAX_CHANGES = 3
const MAX_OPTIONS = 6
const MAX_TAGS = 8
const MAX_CHECKS = 8
const MAX_BODY = 6000
const PR_URL_RE = /^https:\/\/github\.com\/[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}\/pull\/\d{1,9}$/

export interface WelcomeTag { text: string; bad: boolean }
export interface WelcomeOption { label: string; outcome: string }
export interface WelcomeSaid { by: string; why: string; options: WelcomeOption[]; pick: string; pickWhy: string; pickLabel: string }
export interface WelcomeCard {
  /** `item`, `question` or `card`, as the gateway wrote it. */
  kind: string
  /** A work item's id, for an `item` card. */
  itemId: string
  title: string
  tags: WelcomeTag[]
  pr: { label: string; url: string } | null
  read: string
  /** The gateway's "checked HH:MM" line: the card is static once posted. */
  checked: string
  said: WelcomeSaid | null
  /** A question or change card's own option labels. */
  options: string[]
}
export interface WelcomeChange { text: string; tags: WelcomeTag[]; source: string }
export interface WarmWelcome {
  countLine: string
  framing: string
  cards: WelcomeCard[]
  changesTitle: string
  changes: WelcomeChange[]
  more: string
  checks: string[]
  board: { text: string; link: string; conductor: string } | null
  body: string
}

const str = (v: unknown, cap = MAX_TEXT): string => (typeof v === 'string' ? v.trim().slice(0, cap) : '')
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {})
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])

function tagsOf(v: unknown): WelcomeTag[] {
  return arr(v).map(obj).map(t => ({ text: str(t.text), bad: t.bad === true })).filter(t => t.text).slice(0, MAX_TAGS)
}

function saidOf(v: unknown): WelcomeSaid | null {
  const s = obj(v)
  const by = str(s.by)
  if (!by) return null
  const options = arr(s.options).map(obj).map(o => ({ label: str(o.label), outcome: str(o.outcome) })).filter(o => o.label).slice(0, MAX_OPTIONS)
  const pick = options.some(o => o.label === str(s.pick)) ? str(s.pick) : ''
  return { by, why: str(s.why), options, pick, pickWhy: pick ? str(s.pick_why) : '', pickLabel: str(s.pick_label) }
}

function cardOf(v: unknown): WelcomeCard | null {
  const c = obj(v)
  const title = str(c.title)
  if (!title) return null
  const pr = obj(c.pr)
  const url = str(pr.url)
  return {
    kind: str(c.kind, 16),
    itemId: str(c.item_id, 64),
    checked: str(c.checked),
    title,
    tags: tagsOf(c.tags),
    pr: PR_URL_RE.test(url) && str(pr.label) ? { label: str(pr.label), url } : null,
    read: str(c.read),
    said: saidOf(c.said),
    options: arr(c.options).map(o => str(o)).filter(Boolean).slice(0, MAX_OPTIONS),
  }
}

/** The row's gateway-rendered welcome, or null when this row is not one. */
export function warmWelcomeOf(m: ChatMessage): WarmWelcome | null {
  if (m.role !== 'assistant') return null
  const meta = m.meta as { kind?: unknown; brief?: unknown } | undefined
  if (meta?.kind !== 'warm_task_brief_result') return null
  const b = obj(meta.brief)
  if (b.version !== 4) return null
  const countLine = str(b.count_line)
  if (!countLine) return null
  const board = obj(b.board)
  return {
    countLine,
    framing: str(b.framing),
    cards: arr(b.cards).map(cardOf).filter((c): c is WelcomeCard => c !== null).slice(0, MAX_CARDS),
    changesTitle: str(b.changes_title),
    changes: arr(b.changes).map(obj).map(r => ({ text: str(r.text), tags: tagsOf(r.tags), source: str(r.source) })).filter(r => r.text).slice(0, MAX_CHANGES),
    more: str(b.more),
    checks: arr(b.checks).map(c => str(c)).filter(Boolean).slice(0, MAX_CHECKS),
    board: str(board.text) && str(board.conductor, 512) ? { text: str(board.text), link: str(board.link), conductor: str(board.conductor, 512) } : null,
    // Code points, as the gateway counts.
    body: typeof b.body === 'string' ? Array.from(b.body.trim()).slice(0, MAX_BODY).join('') : '',
  }
}

const tagLine = (tags: WelcomeTag[]) => tags.map(t => t.text).join(' · ')
const optionText = (o: WelcomeOption, pick: string, pickLabel: string) =>
  `${o.label}${o.outcome ? `: ${o.outcome}` : ''}${o.label === pick ? ` (${pickLabel})` : ''}`

/** What the card puts on screen, in DOM order, for chat search. */
export function warmWelcomeVisibleText(w: WarmWelcome): string {
  // Mirrors what the card draws, in DOM order, so every search hit has a place.
  const lines = [[w.countLine, w.cards[0]?.title].filter(Boolean).join(' ')]
  if (w.framing) lines.push(w.framing)
  for (const c of w.cards) {
    lines.push([c.pr?.label, c.title].filter(Boolean).join(' '))
    if (c.tags.length) lines.push(tagLine(c.tags))
    if (c.read) lines.push(c.read)
    if (c.checked) lines.push(c.checked)
    if (c.said) {
      if (c.said.why) lines.push(`${c.said.by} ${c.said.why}`)
      lines.push(...c.said.options.map(o => optionText(o, c.said!.pick, c.said!.pickLabel)))
      if (c.said.pick && c.said.pickWhy) lines.push(`${c.said.pickLabel}: ${c.said.pickWhy}`)
    }
    lines.push(...c.options)
  }
  if (w.changes.length) lines.push(w.changesTitle, ...w.changes.map(r => [tagLine(r.tags), r.text, r.source].filter(Boolean).join(' ')))
  if (w.more) lines.push(w.more)
  lines.push(...w.checks)
  if (w.board) lines.push(w.board.text, w.board.link)
  return lines.join('\n')
}

function Tags({ tags }: { tags: WelcomeTag[] }) {
  if (!tags.length) return null
  return (
    <span className="ww-tags" aria-label={i18nT('components.warmBriefCard.tags')}>
      {tags.map((t, i) => <span key={i} className={`ww-tag${t.bad ? ' ww-bad' : ''}`} data-bad={t.bad || undefined}>{t.text}</span>)}
    </span>
  )
}

/** The PR pill never fetches a preview. */
const NO_UNFURL: LinkUnfurl = { enabled: false, live: false }

function Card({ card, messageTs, slotKey }: { card: WelcomeCard; messageTs?: string; slotKey?: string }) {
  const said = card.said
  return (
    <section className="ww-card" data-testid="warm-welcome-card">
      <div className="ww-head">
        {card.pr && (
          <span className="ww-pr" data-testid="warm-welcome-pr">
            {/* The chat's own PR pill for a URL the gateway built from its read; no preview fetch. */}
            <LinkUnfurlCtx.Provider value={NO_UNFURL}>
              <MarkdownRenderer content={card.pr.url} messageTs={messageTs} slotKey={slotKey} />
            </LinkUnfurlCtx.Provider>
          </span>
        )}
        <span className="ww-title">{card.title}</span>
      </div>
      {(card.tags.length > 0 || card.read || card.checked) && (
        <div className="ww-meta">
          <Tags tags={card.tags} />
          {card.read && <span className="ww-read">{card.read}</span>}
          {card.checked && <span className="ww-read" data-testid="warm-welcome-checked">{card.checked}</span>}
        </div>
      )}
      {said && (
        <div className="ww-said" data-testid="warm-welcome-said">
          {said.why && <p className="ww-why"><span className="ww-by">{said.by}</span> {said.why}</p>}
          {said.options.length > 0 && (
            <ul className="ww-options">
              {said.options.map((o, i) => (
                // A plain text row, never a box or a badge: nothing here can be pressed.
                <li key={i} data-pick={o.label === said.pick || undefined}>
                  <span className="ww-option-label">{o.label}</span>
                  {o.outcome && <span className="ww-outcome">: {o.outcome}</span>}
                  {o.label === said.pick && <span className="ww-pick-mark"> ({said.pickLabel})</span>}
                </li>
              ))}
            </ul>
          )}
          {said.pick && said.pickWhy && <p className="ww-pick-why">{said.pickLabel}: {said.pickWhy}</p>}
        </div>
      )}
      {card.options.length > 0 && (
        <ul className="ww-options">{card.options.map((o, i) => <li key={i}><span className="ww-option-label">{o}</span></li>)}</ul>
      )}
    </section>
  )
}

/** Every link in the model's picture draws as its text. */
const inertLink: LinkOverride = ({ children }) => <span>{children}</span>

// Cards are static once posted (owner ruling 2026-11-09): each says when it was
// checked, and live hiding is deferred (RFC 3.11.1).
export default function WarmWelcomeCard({ welcome, messageTs, slotKey }: { welcome: WarmWelcome; messageTs?: string; slotKey?: string }) {
  const w = welcome
  const first = w.cards[0]
  return (
    <div className="ww" data-testid="warm-welcome" role="group" aria-label={i18nT('components.warmBriefCard.label')}>
      <div className="ww-count" data-testid="warm-welcome-count">
        <span className={first ? 'ww-count-chip ww-waits' : 'ww-count-chip'}>{w.countLine}</span>
        {first && <span className="ww-first">{first.title}</span>}
      </div>
      {w.framing && <p className="ww-framing" data-testid="warm-welcome-framing">{w.framing}</p>}
      {w.cards.length > 0 && (
        <div className="ww-cards">{w.cards.map((c, i) => <Card key={i} card={c} messageTs={messageTs} slotKey={slotKey} />)}</div>
      )}
      {w.changes.length > 0 && (
        <div className="ww-changes" data-testid="warm-welcome-changes">
          <div className="ww-section">{w.changesTitle}</div>
          <ul>
            {w.changes.map((r, i) => (
              <li key={i}><Tags tags={r.tags} /> <span>{r.text}</span>{r.source && <span className="ww-read"> {r.source}</span>}</li>
            ))}
          </ul>
          {w.more && <div className="ww-read">{w.more}</div>}
        </div>
      )}
      {w.checks.length > 0 && (
        <ul className="ww-checks" data-testid="warm-welcome-checks">{w.checks.map((c, i) => <li key={i}>{c}</li>)}</ul>
      )}
      {w.body && (
        <div className="min-w-0 break-words" data-testid="warm-welcome-body">
          <LinkOverrideCtx.Provider value={inertLink}>
            <WidgetFrameProfileContext.Provider value="welcome">
              <MarkdownRenderer content={w.body} messageTs={messageTs} slotKey={slotKey} />
            </WidgetFrameProfileContext.Provider>
          </LinkOverrideCtx.Provider>
        </div>
      )}
      {w.board && (
        <div className="ww-board" data-testid="warm-welcome-board">
          <span>{w.board.text}</span>
          <Link to={`/crew-board?conductor=${encodeURIComponent(w.board.conductor)}`} className="ww-board-link">{w.board.link}</Link>
        </div>
      )}
    </div>
  )
}

export function warmWelcomeBodyOf(m: ChatMessage, slotKey?: string): ReactNode | undefined {
  const w = warmWelcomeOf(m)
  return w ? <WarmWelcomeCard welcome={w} messageTs={m.ts} slotKey={slotKey} /> : undefined
}
