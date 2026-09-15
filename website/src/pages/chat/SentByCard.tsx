import { memo } from 'react'
import { Bot, ChevronRight, MessageSquare, Users } from 'lucide-react'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { renderUserContent } from './ChatPageMessageContent'
import { formatTs } from '../../app-sdk/messageRenderers'
import { fmtMessageTimeFull } from './messageTime'
import { isSentByMeta } from '../../utils/sentBy'

/**
 * The gateway's provenance record on a user row another SESSION authored
 * (`session_control.sent_by_meta`). Written by the gateway from the caller's
 * resolved slot, never from the message body, so the fields are trustworthy
 * enough to drive who the row says it is from.
 */
export interface SentBy {
  session_key: string
  via: string
  title?: string
  agent?: string
  member_slug?: string
  /** The sender is a session the receiving thread CREATED (a worker
   *  reporting back). Stamped by the gateway from `_created_by`, so the same
   *  child reads the same whichever tool it reported through. */
  child?: boolean
}

/** Who spoke, in the three shapes the row draws differently. */
export type SentByKind = 'member' | 'worker' | 'session'

/**
 * The provenance line the gateway prepends for the MODEL's benefit
 * (`[sent by session X via session_send]` / `via send_message`). The row hides
 * it from DISPLAY only -- the persisted content, and what any history re-feed
 * shows the model, keep it verbatim.
 */
// Both fields are single tokens (a slot key, a `via` word), matched as
// `[^\s\]]+` so the pattern is linear -- mirrors the backend's
// `_SENT_BY_PREFIX_RE`, which strips the same line before a preview is capped.
const SENT_BY_PREFIX_RE = /^\[sent by session [^\s\]]+ via [^\s\]]+\]\s*/

export function parseSentBy(message: ChatMessage): SentBy | null {
  if (!isSentByMeta(message.meta)) return null
  const rec = (message.meta as { sent_by: Record<string, unknown> }).sent_by
  return {
    session_key: rec.session_key as string,
    via: rec.via as string,
    title: typeof rec.title === 'string' ? rec.title : undefined,
    agent: typeof rec.agent === 'string' ? rec.agent : undefined,
    member_slug: typeof rec.member_slug === 'string' && rec.member_slug ? rec.member_slug : undefined,
    child: rec.child === true ? true : undefined,
  }
}

/** A peer member first; a session this thread created (a worker reporting
 *  back, whichever tool it used) second; any other session last. Keyed on the
 *  RELATIONSHIP the gateway stamped, never on the door the message came
 *  through, so one colleague never wears two badges in one thread. */
export function sentByKind(sentBy: SentBy): SentByKind {
  if (sentBy.member_slug) return 'member'
  if (sentBy.child) return 'worker'
  return 'session'
}

/** The name the header shows after "From". A member is named by its slug, the
 *  thing the roster shows; anything else by its title. A session with no title
 *  is "another session" -- a raw slot key is implementation vocabulary. */
export function sentByName(sentBy: SentBy): string {
  if (sentBy.member_slug) return sentBy.member_slug
  return sentBy.title?.trim() || i18nT('pages.chat.sentByCard.another_session')
}

/** Display body: the row's content minus the model-facing prefix line. */
export function sentByBody(message: ChatMessage): string {
  return stripSentByPrefix(message.content ?? '')
}

/** The same display-only strip for any surface that previews a row's text
 *  (the Members roster's last-message line), so the bracket line the model
 *  reads never leaks into a preview either. Text without the prefix is
 *  returned unchanged. */
export function stripSentByPrefix(text: string): string {
  return text.replace(SENT_BY_PREFIX_RE, '')
}

/** Peer-member rows open by default -- a colleague's message is the point of
 *  the thread. Worker reports and other sessions' rows start folded: they are
 *  status the member acts on, not something the person needs to re-read. */
export function sentByDefaultExpanded(kind: SentByKind): boolean {
  return kind === 'member'
}

const KIND_ICON = { member: Users, worker: Bot, session: MessageSquare } as const

const KIND_LABEL_KEY = {
  member: 'pages.chat.sentByCard.kind_member',
  worker: 'pages.chat.sentByCard.kind_worker',
  session: 'pages.chat.sentByCard.kind_session',
} as const

const KIND_TIP_KEY = {
  member: 'pages.chat.sentByCard.kind_member_tip',
  worker: 'pages.chat.sentByCard.kind_worker_tip',
  session: 'pages.chat.sentByCard.kind_session_tip',
} as const

const PREVIEW_CHARS = 140

function previewLine(body: string): string {
  const firstLine = body.split('\n').find(l => l.trim()) ?? ''
  return firstLine.length > PREVIEW_CHARS ? `${firstLine.slice(0, PREVIEW_CHARS)}…` : firstLine
}

/**
 * A user row another session authored, drawn as a distinct collapsible
 * "From <name>" row rather than as the person's own bubble.
 *
 * Header: chevron · kind badge · "From <name>" · (collapsed) one-line preview ·
 * time. The badge names what kind of sender this is -- a peer member, a
 * worker reporting back, another session -- with a tooltip spelling the door
 * the message came through. The body renders through the same content path a
 * user bubble uses (paste chips, inline images, file cards), minus the
 * provenance prefix line the model reads.
 */
export default memo(function SentByCard({
  message,
  sentBy,
  disclosureKey,
  onFileOpen,
}: {
  message: ChatMessage
  sentBy: SentBy
  disclosureKey?: string
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const kind = sentByKind(sentBy)
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, sentByDefaultExpanded(kind))
  const name = sentByName(sentBy)
  const body = sentByBody(message)
  const Icon = KIND_ICON[kind]
  const ts = formatTs(message.ts)
  const toggleTitle = expanded
    ? i18nT('pages.chat.sentByCard.hide_message')
    : i18nT('pages.chat.sentByCard.show_message')

  return (
    <div
      // `sent-by-card` is a CSS inline-size container (index.css): the folded
      // header drops its preview when the pane is too narrow to show both the
      // sender and a preview, so the "who" keeps the row. Measured on a real
      // pod at 1280x820 with the Crew summary open, the thread pane is 294px and
      // the name was down to 18-24px of its text with the preview still drawn.
      className="sent-by-card w-full max-w-full min-w-0 animate-scale-in"
      data-testid="sent-by-card"
      data-kind={kind}
      data-expanded={expanded ? 'true' : 'false'}
    >
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        aria-expanded={expanded}
        title={toggleTitle}
        className="flex items-center gap-2 w-full min-w-0 text-left text-[12px] leading-5 text-muted hover:text-text transition-colors rounded px-1 -mx-1"
        data-testid="sent-by-card-toggle"
      >
        <ChevronRight
          size={12}
          className={`lucide-inline shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
          aria-hidden="true"
        />
        <span
          className="inline-flex items-center gap-1 shrink-0 rounded-full border border-border px-1.5 py-0 text-[10px] uppercase tracking-wide"
          title={i18nT(KIND_TIP_KEY[kind])}
          data-testid="sent-by-kind-badge"
        >
          <Icon size={10} className="lucide-inline shrink-0" aria-hidden="true" />
          {i18nT(KIND_LABEL_KEY[kind])}
        </span>
        <span
          // Clamped, not `shrink-0`: a sender's auto-title can be a sentence,
          // and a header that pushes the time off the row reads as breakage.
          // The full name (and the sender's agent) stay in the tooltip. The
          // preview beside it carries a much larger flex-shrink, so when the
          // row is short of space the preview gives way first and the name
          // keeps close to its full width up to the 45% cap. Below 28rem of
          // card width (index.css) the preview is gone and the cap is lifted:
          // the name is then the only flexible thing on the row, `min-w-0
          // truncate` still keeps the time in place, and the "who" gets the
          // space the preview gave up instead of a 45% share of it.
          className="sent-by-name min-w-0 max-w-[45%] truncate text-text font-medium"
          title={sentBy.agent ? `${name} · ${i18nT('pages.chat.sentByCard.agent_tip', { agent: sentBy.agent })}` : name}
          data-testid="sent-by-name"
        >
          {
            // A plain session's auto-generated title is a sentence, not a name;
            // quoted, "From “Fix the flaky test”" reads as a title rather than
            // as a broken header. Members and workers keep the bare form.
            kind === 'session' && sentBy.title?.trim()
              ? i18nT('pages.chat.sentByCard.from_quoted', { name })
              : i18nT('pages.chat.sentByCard.from', { name })
          }
        </span>
        {!expanded && (
          <span className="sent-by-preview truncate min-w-0 shrink-[8]" data-testid="sent-by-preview">
            {previewLine(body)}
          </span>
        )}
        {ts && (
          <span className="ml-auto shrink-0 tabular-nums" title={fmtMessageTimeFull(message.ts)}>
            {ts}
          </span>
        )}
      </button>
      {expanded && (
        <div
          className="mt-1 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card px-3 py-2 text-[13px] leading-5 overflow-hidden animate-rise motion-reduce:animate-none"
          style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}
          data-testid="sent-by-body"
        >
          {renderUserContent({ content: body, meta: message.meta, onFileOpen })}
        </div>
      )}
    </div>
  )
})
