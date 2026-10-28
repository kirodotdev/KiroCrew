/**
 * SessionMessageCard — a message this session SENT another Crew session,
 * drawn on the sender's own `session_send` tool row.
 *
 * The words "This session" and a visible "Sent to @{recipient}" beside them,
 * the `@tag` opening the recipient: "This session · Sent to @Planner". Then the
 * body, clamped to two lines until expanded.
 *
 * The receiving side is the ordinary user bubble: `UserMessage` names the
 * sender from the gateway-set `meta.relay`.
 */
import { useState } from 'react'

import MarkdownRenderer from '../../components/MarkdownRenderer'
import { i18nT } from '../../i18n/t'

export interface SessionParty {
  /** The session's own key, so a title can link to it. */
  key: string
  /** The session's display title. */
  title: string
}

export interface SessionMessageCardProps {
  /** This session: named "This session", its title on hover. */
  sender: SessionParty
  /** The recipient, drawn as the `@tag` in the visible "Sent to @{recipient}". */
  recipient: SessionParty
  /** The message text (Markdown), clamped to two lines until expanded. */
  body: string
  /** The row's preformatted time, drawn as the muted meta line; absent, no line. */
  time?: string | null
  /** Open a session by key. Given, the `@tag` is a real link; absent, plain
   *  text — a host that cannot navigate keeps a non-interactive card rather
   *  than a dead-looking link. */
  onOpenSession?: (key: string) => void
}

/** Placeholder passed as the name in the translated "Sent to {{name}}" and
 *  split back out, so the `@tag` lands wherever the language puts the name. A
 *  machine token, never shown. */
const RECIPIENT_SLOT = 'session_card_recipient_slot'

/** The card's visible direction: "Sent to @{recipient}", in the locale's own
 *  word order, with the `@tag` in the name's place. */
function SentTo({ recipient, onOpenSession }: { recipient: SessionParty; onOpenSession?: (key: string) => void }) {
  const phrase = i18nT('pages.chat.sessionMessageCard.to_recipient', { name: RECIPIENT_SLOT })
  const at = phrase.indexOf(RECIPIENT_SLOT)
  const before = at >= 0 ? phrase.slice(0, at) : `${phrase} `
  const after = at >= 0 ? phrase.slice(at + RECIPIENT_SLOT.length) : ''
  return (
    <span className="min-w-0 text-[11.5px] text-muted" data-testid="session-card-direction">
      {before}
      <RecipientTag recipient={recipient} onOpenSession={onOpenSession} />
      {after}
    </span>
  )
}

/** The recipient `@tag` in the card's "Sent to …". */
function RecipientTag({ recipient, onOpenSession }: { recipient: SessionParty; onOpenSession?: (key: string) => void }) {
  const label = `@${recipient.title}`
  const cls = 'inline-flex items-center align-middle rounded px-1.5 py-px text-[11px] font-medium bg-accent/15 text-accent'
  if (onOpenSession && recipient.key) {
    return (
      <button
        type="button"
        className={`${cls} border-none cursor-pointer hover:bg-accent/25`}
        onClick={() => onOpenSession(recipient.key)}
        aria-label={i18nT('pages.chat.sessionMessageCard.open_session', { name: recipient.title })}
        data-testid="session-card-recipient"
      >
        {label}
      </button>
    )
  }
  return (
    <span className={cls} data-testid="session-card-recipient">
      {label}
    </span>
  )
}

export function SessionMessageCard({ sender, recipient, body, time = null, onOpenSession }: SessionMessageCardProps) {
  const [open, setOpen] = useState(false)
  // Clamp a body that visibly runs past ~two lines; a short one never offers the
  // toggle.
  const long = body.length > 160 || body.split('\n').length > 2
  const shownTime = time && time.trim() ? time : null

  return (
    <div
      className="rounded-lg border border-border bg-bg-elevated px-3 py-2 flex flex-col gap-1"
      data-testid="session-message-card"
      data-direction="outgoing"
    >
      <div className="flex items-center gap-2 min-w-0">
        <div className="flex items-center gap-1.5 min-w-0 flex-wrap">
          {/* The title IS this session, so it is plain text, never a self-link;
              "This session" reads right in its own chat, the title stays on hover. */}
          <span className="min-w-0 truncate text-[12px] font-medium text-text-strong" title={sender.title} data-testid="session-card-title">
            {i18nT('pages.chat.sessionMessageCard.this_session')}
          </span>
          <span className="text-[11.5px] text-muted" aria-hidden="true">·</span>
          <SentTo recipient={recipient} onOpenSession={onOpenSession} />
        </div>
      </div>

      {shownTime ? (
        <p className="m-0 text-[11px] text-muted truncate tabular-nums" data-testid="session-card-meta">
          {shownTime}
        </p>
      ) : null}

      <div
        className={`text-[12.5px] text-text break-words [&_p]:m-0 ${long && !open ? 'line-clamp-2' : ''}`}
        data-testid="session-card-body"
      >
        <MarkdownRenderer content={body} />
      </div>

      {long ? (
        <button
          type="button"
          className="self-start bg-transparent border-none p-0 text-[11.5px] text-accent cursor-pointer hover:underline"
          onClick={() => setOpen(v => !v)}
          aria-expanded={open}
          data-testid="session-card-toggle"
        >
          {open ? i18nT('pages.chat.sessionMessageCard.show_less') : i18nT('pages.chat.sessionMessageCard.show_more')}
        </button>
      ) : null}
    </div>
  )
}

export default SessionMessageCard
