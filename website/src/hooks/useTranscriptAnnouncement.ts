/**
 * The text the transcript's live region should be holding right now.
 *
 * Owns the one decision that makes a chat live region bearable: WHEN to speak.
 * The region itself is in `TranscriptScrollShell`, which is deliberately not the
 * scroller — see the comment on that element for why a virtualized container
 * cannot carry `aria-live` at all.
 *
 * Speaks on the streaming -> settled TRANSITION and nowhere else. Announcing per
 * flush reads a growing reply over and over, which is the behaviour this
 * replaces; announcing whenever the transcript merely ENDS in an assistant row
 * reads an old reply back on first load and on every session switch. Tracking
 * the previous streaming state is what separates a turn that landed here from a
 * transcript that happens to end in one.
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

import { i18nT } from '../i18n/t'
import { messageAnnouncementText } from '../utils/messageAnnouncement'

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
  /** Speak the user's own message; call it when a send puts the message in the transcript. */
  announceSent: (markdown: string) => void
}

export function useTranscriptAnnouncement(
  lastMsg: AnnouncementTail | undefined,
  isStreaming: boolean,
  /** The name a reply is spoken under when it is not the product's (a
   *  crewmate's chat). Matches `AssistantMessage`'s `speakerName`. */
  assistantName?: string,
): TranscriptAnnouncement {
  const [announcement, setAnnouncement] = useState('')
  const wasStreamingRef = useRef(false)
  const releaseRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  // Whether the region holds the user's sent message. The reply's streaming
  // start must not clear that one: it usually begins well under a second after
  // the send, and the reader should hear what they sent in full.
  const holdingSentRef = useRef(false)
  const hold = useCallback((text: string, sent: boolean) => {
    clearTimeout(releaseRef.current)
    holdingSentRef.current = sent
    setAnnouncement(text)
    releaseRef.current = setTimeout(() => {
      holdingSentRef.current = false
      setAnnouncement('')
    }, ANNOUNCEMENT_HOLD_MS)
  }, [])
  useEffect(() => () => clearTimeout(releaseRef.current), [])
  const announceSent = useCallback((markdown: string) => {
    if (!markdown.trim()) return
    hold(spoken(i18nT('pages.chat.userMessage.speaker_heading'), markdown), true)
  }, [hold])
  const role = lastMsg?.role
  const content = lastMsg?.content
  useEffect(() => {
    const landed = wasStreamingRef.current && !isStreaming
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
    if (!landed || role !== 'assistant') return
    hold(spoken(assistantName || i18nT('pages.chat.assistantMessage.speaker_heading'), content ?? ''), false)
  }, [isStreaming, role, content, hold, assistantName])
  return { announcement, announceSent }
}
