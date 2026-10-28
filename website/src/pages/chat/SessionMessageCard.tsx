/**
 * SessionMessageCard — the one card for a message that crossed between two Crew
 * sessions, drawn in BOTH directions.
 *
 * The transcript draws it for any `session_send` / `session_broadcast` between
 * two sessions: the receiver's bubble (INCOMING) and the sender's own tool row
 * (OUTGOING). Any other caller with a sender, a body and an optional recipient
 * passes the same props.
 *
 * INCOMING (this session received it): a filled avatar with the SENDER's
 * initials on its deterministic colour, the sender's title as a link that opens
 * that session (or its step in the run view), muted meta (agent · run · time),
 * and the body clamped to two lines. An incoming step message whose step waits
 * on the person carries a "Needs you" pill.
 *
 * OUTGOING (this session sent it): the SENDER (this session) in an OUTLINED
 * avatar with a ↖ badge, its own title, and a body that opens with the
 * recipient as an accent `@tag`, then the text. Same clamp.
 *
 * Direction is never colour-only: the outgoing avatar carries the arrow badge
 * and an sr-only "to {name}", the incoming one is filled. The avatar is
 * decorative (aria-hidden) — the sender's name is always in the text beside it,
 * and the title link carries a real accessible name.
 */
import { useState } from 'react'
import { ArrowUpLeft } from 'lucide-react'

import MarkdownRenderer from '../../components/MarkdownRenderer'
import { senderAvatarColor, senderInitials } from '../../utils/sessionColors'
import { i18nT } from '../../i18n/t'

export interface SessionParty {
  /** The session's own key, so a title can link to it. */
  key: string
  /** The session's display title. */
  title: string
}

export interface SessionMessageCardProps {
  direction: 'incoming' | 'outgoing'
  /** The party whose avatar and title head the card: the SENDER either way —
   *  the other session for incoming, this session for outgoing. */
  sender: SessionParty
  /** Outgoing only: the recipient drawn as the body's leading `@tag`. */
  recipient?: SessionParty | null
  /** The message text (Markdown), clamped to two lines until expanded. */
  body: string
  /** Muted meta on an incoming card: the sender's agent, the run name, a
   *  preformatted time. Absent parts are dropped. */
  agent?: string | null
  runName?: string | null
  time?: string | null
  /** Open a session (or a step) by key. Given, the title / `@tag` become real
   *  links; absent, they are plain text — a host that cannot navigate keeps a
   *  non-interactive card rather than a dead-looking link. */
  onOpenSession?: (key: string) => void
  /** INCOMING step message whose step is parked for the person: the pill. */
  needsYou?: boolean
}

/** The deterministic coloured avatar. Filled for incoming, outlined with a ↖
 *  badge for outgoing. Decorative — `aria-hidden`, name is in the text. */
function Avatar({ sessionKey, title, outgoing }: { sessionKey: string; title: string; outgoing: boolean }) {
  const { bg, fg } = senderAvatarColor(sessionKey)
  const initials = senderInitials(title)
  if (outgoing) {
    return (
      <span className="relative shrink-0" aria-hidden="true" data-testid="session-card-avatar" data-outgoing="true">
        <span
          className="flex items-center justify-center w-6 h-6 rounded-full border-[1.5px] bg-transparent text-[10px] font-semibold tabular-nums"
          style={{ borderColor: bg, color: bg }}
        >
          {initials}
        </span>
        <span
          className="absolute -top-1 -left-1 flex items-center justify-center w-3.5 h-3.5 rounded-full border border-border bg-card"
          style={{ color: bg }}
        >
          <ArrowUpLeft size={9} strokeWidth={2.5} />
        </span>
      </span>
    )
  }
  return (
    <span
      className="flex items-center justify-center shrink-0 w-6 h-6 rounded-full text-[10px] font-semibold tabular-nums"
      style={{ background: bg, color: fg }}
      aria-hidden="true"
      data-testid="session-card-avatar"
      data-outgoing="false"
    >
      {initials}
    </span>
  )
}

/** The sender's title: a link that opens the session when the host wired
 *  `onOpenSession`, else plain text. */
function TitleLink({ party, onOpenSession, incoming }: { party: SessionParty; onOpenSession?: (key: string) => void; incoming: boolean }) {
  if (onOpenSession && party.key) {
    return (
      <button
        type="button"
        className="min-w-0 truncate bg-transparent border-none p-0 text-[12px] font-medium text-left cursor-pointer text-accent hover:underline"
        onClick={() => onOpenSession(party.key)}
        aria-label={i18nT('pages.chat.sessionMessageCard.open_session', { name: party.title })}
        data-testid="session-card-title-link"
      >
        {party.title}
      </button>
    )
  }
  // Non-interactive: the sender's own title on an outgoing card, or a host with
  // nowhere to navigate.
  return (
    <span className={`min-w-0 truncate text-[12px] font-medium ${incoming ? 'text-text-strong' : 'text-text-strong'}`} data-testid="session-card-title">
      {party.title}
    </span>
  )
}

/** The recipient `@tag` that opens an outgoing card's body. */
function RecipientTag({ recipient, onOpenSession }: { recipient: SessionParty; onOpenSession?: (key: string) => void }) {
  const label = `@${recipient.title}`
  const cls = 'inline-flex items-center align-middle mr-1 rounded px-1.5 py-px text-[11px] font-medium bg-accent/15 text-accent'
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

export function SessionMessageCard({
  direction,
  sender,
  recipient = null,
  body,
  agent = null,
  runName = null,
  time = null,
  onOpenSession,
  needsYou = false,
}: SessionMessageCardProps) {
  const outgoing = direction === 'outgoing'
  const [open, setOpen] = useState(false)
  // Clamp a body that visibly runs past ~two lines; a short one never offers the
  // toggle.
  const long = body.length > 160 || body.split('\n').length > 2
  const metaParts = [agent, runName, time].filter((p): p is string => !!p && !!p.trim())

  return (
    <div
      className="rounded-lg border border-border bg-bg-elevated px-3 py-2 flex flex-col gap-1"
      data-testid="session-message-card"
      data-direction={direction}
    >
      <div className="flex items-center gap-2 min-w-0">
        <Avatar sessionKey={sender.key} title={sender.title} outgoing={outgoing} />
        <div className="flex items-center gap-1.5 min-w-0 flex-wrap">
          {/* Incoming: the sender's title opens that session. Outgoing: the
              title IS this session, so it is plain text, never a self-link. */}
          <TitleLink party={sender} onOpenSession={outgoing ? undefined : onOpenSession} incoming={!outgoing} />
          {needsYou ? (
            <span
              className="shrink-0 rounded-full px-1.5 py-px text-[10px] font-medium bg-warn/15 text-warn border border-warn/30"
              data-testid="session-card-needs-you"
            >
              {i18nT('pages.chat.sessionMessageCard.needs_you')}
            </span>
          ) : null}
        </div>
        {/* Direction is conveyed by the avatar (filled vs outlined + arrow); this
            names it for assistive tech on the outgoing card. */}
        {outgoing && recipient ? (
          <span className="sr-only" data-testid="session-card-direction-sr">
            {i18nT('pages.chat.sessionMessageCard.to_recipient', { name: recipient.title })}
          </span>
        ) : null}
      </div>

      {metaParts.length > 0 ? (
        <p className="m-0 text-[11px] text-muted truncate tabular-nums" data-testid="session-card-meta">
          {metaParts.join(' · ')}
        </p>
      ) : null}

      {/* The @tag opens the outgoing body and the markdown paragraphs are
          inlined so the chip sits with the first line and the clamp still bounds
          two visual lines. */}
      <div
        // Outgoing: the renderer's own root <div> and the first paragraph both go
        // inline, else the block root drops the text onto the line below the tag.
        className={`text-[12.5px] text-text break-words [&_p]:m-0 ${outgoing ? '[&>div]:inline [&_p]:inline' : ''} ${long && !open ? 'line-clamp-2' : ''}`}
        data-testid="session-card-body"
      >
        {outgoing && recipient ? <RecipientTag recipient={recipient} onOpenSession={onOpenSession} /> : null}
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
