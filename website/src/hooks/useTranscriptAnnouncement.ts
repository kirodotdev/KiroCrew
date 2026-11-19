/**
 * The text the transcript's live region should be holding right now.
 *
 * Owns the one decision that makes a chat live region bearable: WHEN to speak.
 * The region itself is in `TranscriptScrollShell`, which is deliberately not the
 * scroller — see the comment on that element for why a virtualized container
 * cannot carry `aria-live` at all.
 *
 * Speaks a reply, or the error a turn ended in, on the streaming -> settled
 * TRANSITION. Announcing per flush reads a growing reply over and over, which is
 * the behaviour this replaces; announcing whenever the transcript merely ENDS in
 * an assistant row reads an old reply back on first load and on every session
 * switch. Tracking the previous streaming state is what separates a turn that
 * landed here from a transcript that happens to end in one.
 *
 * A row that arrives already finished has no transition: a Telegram or Discord
 * turn is appended whole, and a turn can fail before it streams. Such a row is
 * spoken when it is a new tail row by identity. The first transcript a session
 * shows is its baseline and is never spoken, so first load and a session switch
 * stay silent with no clock involved: the gateway's timestamps and the
 * browser's clock are never compared.
 *
 * The user's own message is spoken too, at the moment it is sent, so a reader
 * hears what went out before the reply comes back. That one is driven by the
 * send itself (`announceSent`) rather than by watching the transcript, because
 * a user row at the tail is also what a session that is still running looks
 * like on first load, and only the send knows the row is new.
 *
 * Both are prefixed with the same speaker label the transcript's offscreen
 * headings use, so what is heard matches what H navigation lands on.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { fmtList } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { isInvisibleOnly } from '../utils/invisibleText'
import { isSystemNoticeKind } from '../lib/systemNotice'
import { pathBasename, stripTrailingSeparators } from '../utils/pathBasename'
import { resolveGatewayNotice } from '../pages/chat/gatewayNotice'
import type { ChatMessage } from '../types'
import { messageAnnouncementText } from '../utils/messageAnnouncement'
import { extractInlineThinking } from '../app-sdk/protocol/inlineThinking'
import { extractSteeringAcks, parseOptions } from '../app-sdk/protocol'
import { parseGrill } from '../app-sdk/protocol/grillMarker'
import { isLocalSendId } from '../utils/sendDelivery'

/**
 * How long the region keeps a settled reply before releasing it, in ms.
 *
 * Two pressures set this. It must outlast the moment a screen reader takes the
 * region's new text — that happens on the mutation, so any delay suffices and
 * clearing later cannot cut off speech already under way. And it must be short,
 * because while the text is there it is a SECOND copy of the reply in the DOM,
 * sitting ahead of the whole transcript in browse order. Five seconds is far
 * past any capture latency and over before a reader who has finished listening
 * starts arrowing back through the conversation.
 */
export const ANNOUNCEMENT_HOLD_MS = 5000

/** The tail of the transcript, as much of it as this decision reads. */
export interface AnnouncementTail {
  role?: string
  content?: string
  /** The server timestamp, one of the row's identities (`rowIdentity`). */
  ts?: string
  /** The row's kind (a system notice such as `compaction`), when it has one. */
  kind?: string
  /** Carries the row's identities (`clientTs`, `mid`) and, on some rows, its kind. */
  meta?: Record<string, unknown> | null
}

/**
 * The transcript's last row as the live region should read it. A gateway
 * status row (a timeout, a retry) is drawn from localized copy keyed on its
 * notice token rather than from its English `content`, so its spoken text is
 * that same copy (`resolveGatewayNotice`); every other row is passed through.
 */
export function announcementTail(messages: readonly ChatMessage[]): ChatMessage | undefined {
  const index = messages.length - 1
  const last = messages[index]
  if (!last) return undefined
  const notice = resolveGatewayNotice(last, messages, index)
  return notice ? { ...last, content: notice.text } : last
}

/**
 * A row's identity, the same key the renderer uses: `meta.clientTs` (a row
 * born in this browser, carried onto its server copy by the reload), then the
 * server `mid`, then the server `ts`. A row the browser added with none of
 * them (a local send failure) falls back to its position, which a newly
 * appended row always changes.
 */
export function rowIdentity(row: AnnouncementTail | undefined, rowCount: number): string {
  if (!row) return ''
  const meta = row.meta ?? {}
  for (const v of [meta.clientTs, meta.mid, row.ts, meta.sendId]) {
    if ((typeof v === 'string' && v) || typeof v === 'number') return String(v)
  }
  return `#${rowCount}`
}

/**
 * The part of an assistant reply its bubble actually shows as prose, in the
 * order `AssistantMessage` strips the rest: collapsed inline reasoning, a
 * `[GRILL:]` card, the `[OPTIONS:]` pills, and steering acknowledgments.
 * Speaking the raw content would read those out as if they were the answer.
 */
export function assistantSpokenSource(content: string): string {
  const thinkingCleaned = extractInlineThinking(content).cleaned
  const grillCleaned = parseGrill(thinkingCleaned).text
  return extractSteeringAcks(parseOptions(grillCleaned).text).cleaned
}

/** One approval the agent is waiting on, as the live region needs it. */
export interface PendingApproval {
  /** The request's identity, so each one is spoken once. */
  id: string
  /** The short operation title the approval card shows. */
  label: string
}

/**
 * The approvals still waiting in a transcript: every `permission` row without
 * a `resolved` decision. The whole transcript, not only its tail, because a
 * tool row can land after the request it is waiting on.
 */
export function pendingApprovalsOf(
  messages: ReadonlyArray<{ role?: string, content?: string, ts?: string, meta?: Record<string, unknown> | null }>,
): PendingApproval[] {
  const out: PendingApproval[] = []
  for (const m of messages) {
    if (m.role !== 'permission' || m.meta?.resolved) continue
    // The row's own identity, not the request id: a request id can be reused
    // by a later request in the same session, and keying on it would leave
    // that new request unspoken while the agent waits.
    const mid = m.meta?.mid
    const approvalId = m.meta?.approval_id
    const id = typeof mid === 'string' && mid
      ? mid
      : [typeof approvalId === 'string' ? approvalId : '', m.ts ?? ''].filter(Boolean).join('@')
    if (!id) continue
    const purpose = typeof m.meta?.purpose === 'string' ? m.meta.purpose.trim() : ''
    // The row's content is `[<source>] <tool>`; the source tag is not the operation.
    const label = purpose || (m.content ?? '').replace(/^\[[^\]]*\]\s*/, '').split('\n')[0].trim()
    out.push({ id, label })
  }
  return out
}

/** A sent message as its bubble shows it. The wire text carries one
 *  `[attached_file N] <path>` (or `[attached_dir N]`) line per attachment,
 *  which the bubble draws as a card named after the file; the file's name is
 *  what is spoken in its place, never the marker or the full path. */
export function sentSpokenSource(markdown: string): string {
  return markdown.replace(/^\[attached_(?:file|dir) \d+\] (.+)$/gm, (_m, path: string) => {
    // The shared path helpers: a backslash separates segments only in a
    // Windows-shaped path, so a POSIX file named `foo\bar` keeps its name.
    const name = pathBasename(stripTrailingSeparators(path.trim()))
    return name || path.trim()
  })
}

/** "<speaker>: <text>", with the speaker label the offscreen headings use. */
function spoken(speaker: string, markdown: string): string {
  return i18nT('pages.chat.transcriptAnnouncement.spoken', {
    speaker,
    text: messageAnnouncementText(markdown),
  })
}

export interface TranscriptAnnouncement {
  /** The text the live region should hold right now. */
  announcement: string
  /** Set while `announcement` speaks the transcript's newest row: its speaker,
   *  so the live region can read that row as drawn (`TranscriptLiveRegion`)
   *  and keep `announcement` as the fallback. `null` otherwise. */
  announcementTailSpeaker: string | null
  /** Speak the user's own message; call it when a send puts the message in the transcript. */
  announceSent: (markdown: string) => void
}

export function useTranscriptAnnouncement(
  lastMsg: AnnouncementTail | undefined,
  isStreaming: boolean,
  /** The name a reply is spoken under when it is not the product's (a
   *  crewmate's chat). Matches `AssistantMessage`'s `speakerName`. */
  assistantName?: string,
  /** The session this transcript belongs to. A host that mounts the hook once
   *  for every session (the chat page) passes it, so a switch is not taken for
   *  a landing. */
  sessionKey?: string | null,
  /** The approvals waiting in this transcript (`pendingApprovalsOf`). A new
   *  one is spoken once, ahead of anything else the region holds. */
  pendingApprovals?: readonly PendingApproval[],
  /** How many rows the transcript shows, for a row with no identity of its own
   *  (`rowIdentity`). */
  rowCount = 0,
): TranscriptAnnouncement {
  const [announcement, setAnnouncement] = useState('')
  const [announcementTailSpeaker, setTailSpeaker] = useState<string | null>(null)
  const wasStreamingRef = useRef(false)
  const releaseRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  // Whether the region holds the user's sent message. The reply's streaming
  // start must not clear that one: it usually begins well under a second after
  // the send, and the reader should hear what they sent in full.
  const holdingSentRef = useRef(false)
  const sessionKeyRef = useRef(sessionKey)
  // The session the held sent message was sent from, and the session shown on
  // this render (read by `announceSent`, which runs outside the effects).
  const sentSessionRef = useRef<string | null | undefined>(undefined)
  // The text of the reader's own last send, so its row is not spoken again
  // when it reaches the transcript.
  const lastSentRef = useRef('')
  const renderedKeyRef = useRef(sessionKey)
  renderedKeyRef.current = sessionKey
  // Whether what the region holds is an approval, which belongs to the session
  // that raised it and is dropped on a switch even though it is held like a
  // sent message.
  const holdingApprovalRef = useRef(false)
  const hold = useCallback((text: string, sent: boolean, approval = false, tailSpeaker: string | null = null) => {
    clearTimeout(releaseRef.current)
    holdingSentRef.current = sent
    holdingApprovalRef.current = approval
    setTailSpeaker(tailSpeaker)
    // A live region speaks a CHANGE. When the text equals what it already
    // holds (the same message sent twice inside the hold), a trailing no-break
    // space, which is not spoken, makes the second one a change too.
    setAnnouncement(prev => (prev === text ? text + String.fromCharCode(0xa0) : text))
    releaseRef.current = setTimeout(() => {
      holdingSentRef.current = false
      holdingApprovalRef.current = false
      setAnnouncement('')
      setTailSpeaker(null)
    }, ANNOUNCEMENT_HOLD_MS)
  }, [])
  useEffect(() => () => clearTimeout(releaseRef.current), [])
  const announceSent = useCallback((markdown: string) => {
    if (isInvisibleOnly(messageAnnouncementText(sentSpokenSource(markdown)))) return
    sentSessionRef.current = renderedKeyRef.current
    lastSentRef.current = sentSpokenSource(markdown).trim()
    hold(spoken(i18nT('pages.chat.userMessage.speaker_heading'), sentSpokenSource(markdown)), true)
  }, [hold])
  const role = lastMsg?.role
  // A system notice (compaction, a session reload) is an assistant row the
  // transcript draws as a status card, not a reply, so it is never spoken as one.
  const systemNotice = isSystemNoticeKind(lastMsg?.kind ?? (lastMsg?.meta?.kind as string | undefined))
  const content = lastMsg?.content
  const tailId = rowIdentity(lastMsg, rowCount)
  const tailMid = typeof lastMsg?.meta?.mid === 'string' ? lastMsg.meta.mid : ''
  // Whether the tail row is the reader's own by its marks (not its text), and
  // whether it carries an author stamp, which only another sender's row has.
  const tailSendId = lastMsg?.meta?.sendId
  const tailMarkedOwn = lastMsg?.meta?.optimistic === true || isLocalSendId(tailSendId)
  const tailAuthored = !!lastMsg?.meta?.author
  // The tail identities this session has shown, whether this session's baseline
  // (its first non-empty transcript) has been taken, and what was last spoken
  // from the tail. Together they let a row that arrives already finished be
  // spoken once without reading history back, and keep a row that becomes the
  // tail again (an optimistic row after it is dropped) from being spoken twice.
  const seenTailsRef = useRef<Set<string>>(new Set())
  // The tails already shown when the current turn started. A turn that ends
  // with one of them still at the tail (a send that failed, a turn stopped
  // before it wrote anything) did not land a reply, so nothing is spoken.
  const preTurnSeenRef = useRef<Set<string>>(new Set())
  const tailBaselineRef = useRef(false)
  const lastSpokenTailRef = useRef({ said: '', mid: '', count: -1 })
  useEffect(() => {
    // A session switch starts the streaming history over. Without this, leaving
    // a session mid-stream for an idle one reads as streaming -> settled and the
    // idle session's old reply is spoken. A reply still held from the session
    // left behind is dropped. A held sent message is kept only when it was sent
    // from a chat with no session yet, which gets its key on that very send;
    // one sent from another session does not belong to the one now shown.
    if (sessionKeyRef.current !== sessionKey) {
      const keepSent = holdingSentRef.current && (!sentSessionRef.current || sentSessionRef.current === sessionKey)
      sessionKeyRef.current = sessionKey
      wasStreamingRef.current = false
      tailBaselineRef.current = false
      seenTailsRef.current = new Set()
      preTurnSeenRef.current = new Set()
      lastSpokenTailRef.current = { said: '', mid: '', count: -1 }
      if (!keepSent) {
        clearTimeout(releaseRef.current)
        holdingSentRef.current = false
        holdingApprovalRef.current = false
        setAnnouncement('')
      }
    }
    // The first non-empty transcript is the baseline: recorded, never spoken.
    const baseline = tailId !== '' && !tailBaselineRef.current
    if (baseline) tailBaselineRef.current = true
    const seenTails = seenTailsRef.current
    if (isStreaming && !wasStreamingRef.current) preTurnSeenRef.current = new Set(seenTails)
    const newTail = !baseline && tailId !== '' && !seenTails.has(tailId)
    if (tailId !== '') seenTails.add(tailId)
    // A position (`#n`) is not proof the row is the same one, so only a real
    // identity counts as "already there before the turn".
    const sameAsBeforeTurn = !tailId.startsWith('#') && preTurnSeenRef.current.has(tailId)
    const landed = wasStreamingRef.current && !isStreaming && !sameAsBeforeTurn
    wasStreamingRef.current = isStreaming
    // Cleared while a turn streams, so the next landing is a CHANGE the region
    // speaks even when two replies read identically. A held sent message is
    // left alone; the reply's text differs from it, so it is still a change.
    if (isStreaming) {
      if (!holdingSentRef.current) {
        clearTimeout(releaseRef.current)
        setAnnouncement('')
      }
      return
    }
    if (systemNotice) return
    const delivered = !landed && newTail
    // A notice row (a delivery warning) draws as a status card with no speaker,
    // so its text is spoken alone, from the row's own content.
    if (role === 'notice') {
      if (!landed && !delivered) return
      const text = messageAnnouncementText(content ?? '')
      if (!isInvisibleOnly(text)) hold(text, false)
      return
    }
    // A user-role row is someone else's message (a teammate, an app, another
    // session) only when it arrives on its own: the reader's own send was
    // spoken by `announceSent`. Its optimistic row, a row carrying an id this
    // tab minted, and a row carrying the text just sent are that send. A
    // teammate's row carries THEIR `sendId`, so any id is not proof. The text
    // match covers a send path that carries no id: it applies only to a row
    // with no author stamp (a teammate's always has one) and only once, so a
    // later message that happens to read the same is still spoken.
    const echoOfSent = role === 'user' && !tailAuthored && lastSentRef.current !== ''
      && sentSpokenSource(content ?? '').trim() === lastSentRef.current
    const ownSend = role === 'user' && (
      tailMarkedOwn || echoOfSent
    )
    // Used up only by the copy the text match alone identified (the server
    // copy of an id-less send), never by the optimistic row before it. A row
    // carrying this tab's id means the send is on an id path, so its copies
    // are recognised by the id and the text match is not needed again.
    if (echoOfSent && delivered && !tailMarkedOwn) lastSentRef.current = ''
    if (role === 'user' && isLocalSendId(tailSendId)) lastSentRef.current = ''
    if (role === 'user' ? !delivered || ownSend : role !== 'assistant' && role !== 'error') return
    if (!landed && !delivered) return
    // An error row's content is the prose its card shows; a reply's is read
    // the way its bubble shows it. A row whose text is invisible (a quiet
    // monitor cycle's zero-width reply, which the renderer does not draw) is
    // not spoken: there is no message on screen to name a speaker for.
    const source = role === 'assistant' ? assistantSpokenSource(content ?? '') : role === 'user' ? sentSpokenSource(content ?? '') : (content ?? '')
    if (isInvisibleOnly(messageAnnouncementText(source))) return
    const said = `${role}\n${source}`
    // The row just spoken, back from the reload as a server copy that did not
    // keep its browser identity, is not a new row: it is at the same position,
    // reads the same, and is the first copy to carry a `mid`. Anything at
    // another position, an intervening turn included, is a different row.
    const last = lastSpokenTailRef.current
    if (delivered && !last.mid && tailMid !== '' && rowCount === last.count && said.trim() === last.said.trim()) return
    lastSpokenTailRef.current = { said, mid: tailMid, count: rowCount }
    const speaker = role === 'user'
      ? i18nT('pages.chat.userMessage.speaker_heading')
      : assistantName || i18nT('pages.chat.assistantMessage.speaker_heading')
    hold(spoken(speaker, source), false, false, speaker)
  }, [isStreaming, role, systemNotice, content, tailId, tailMid, tailMarkedOwn, tailAuthored, tailSendId, rowCount, hold, assistantName, sessionKey])
  // Approvals. The scroller's old live region read a permission row as it
  // appeared; without it, and with the banner suppressed for the chat on
  // screen, the agent would sit blocked with nothing said. Each request is
  // spoken once. Requests in a session's first non-empty transcript (first
  // load, a switch) are recorded silently, as an old reply is: the approvals
  // ride the same rows as the tail, so that render is the load.
  const seenApprovalsRef = useRef<Set<string>>(new Set())
  const approvalsSessionRef = useRef<string | null | undefined>(undefined)
  const approvalsArmedRef = useRef(false)
  const hasRows = tailId !== ''
  const approvalKey = (pendingApprovals ?? []).map(a => a.id).join('\n')
  useEffect(() => {
    const list = pendingApprovals ?? []
    if (approvalsSessionRef.current !== sessionKey) {
      approvalsSessionRef.current = sessionKey
      approvalsArmedRef.current = false
      seenApprovalsRef.current = new Set()
      if (holdingApprovalRef.current) {
        clearTimeout(releaseRef.current)
        holdingSentRef.current = false
        holdingApprovalRef.current = false
        setAnnouncement('')
      }
    }
    const seen = seenApprovalsRef.current
    const fresh = list.filter(a => !seen.has(a.id))
    for (const a of fresh) seen.add(a.id)
    if (!approvalsArmedRef.current) {
      if (hasRows) approvalsArmedRef.current = true
      return
    }
    if (!fresh.length) return
    // Held like a sent message: a reply starting to stream must not clear it,
    // because the agent is blocked until the reader acts on it. Requests that
    // arrive together are named together, so none is marked seen unspoken.
    hold(i18nT('pages.chat.transcriptAnnouncement.approval', {
      speaker: assistantName || i18nT('pages.chat.assistantMessage.speaker_heading'),
      operation: fmtList(fresh.map(a => a.label)),
    }), true, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- approvalKey stands in for the list identity
  }, [approvalKey, sessionKey, hasRows, hold, assistantName])
  return { announcement, announcementTailSpeaker, announceSent }
}
