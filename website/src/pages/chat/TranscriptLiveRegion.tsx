/**
 * The transcript's one live region.
 *
 * It is always in the page and only its text changes, the one pattern screen
 * readers announce reliably: a live element inserted with its text already in
 * it is often not spoken at all.
 *
 * When the text is about the transcript's newest row (`tailSpeaker` set), the
 * region speaks that row as the screen DREW it, read from the rendered bubble
 * or card, so what is heard is what is shown: a widget's title, a translated
 * error card, sanitized HTML, rendered math. `announcement` is the fallback for
 * when that row is not mounted (the reader has scrolled up, so the virtualizer
 * dropped it) or draws no text.
 */
import React, { useEffect, useRef, useState } from 'react'

import { i18nT } from '../../i18n/t'
import { isTranscriptRow } from './usePinnedPrompt'

/** The elements a row's message is drawn in: the bubble (`message-bubble` is
 *  the stable theming hook on user and assistant bubbles) and the cards that
 *  stand in for a bubble on error and notice rows. */
const MESSAGE_ELEMENTS = '[class~="message-bubble"],[data-testid="error-card"],[data-testid="notice-card"]'

/** Parts of a drawn message a reader does not hear as its text: offscreen
 *  labels, decorative or duplicate markup, and controls (a code block's Copy). */
const NOT_SPOKEN = '[class~="sr-only"],[aria-hidden="true"],button,annotation,script,style,template'

/** How long after an announcement arrives the newest row is read, in ms: long
 *  enough for the virtualizer to mount and measure a just-appended row, short
 *  enough that the reader hears the reply as it lands. */
export const TAIL_READ_DELAY_MS = 50

/** The newest drawn message: its text, and the speaker its offscreen heading
 *  names (the `h2` each user and assistant row opens with), or `''` for either
 *  when the screen has none to read. */
export function renderedTail(scroller: HTMLElement | null): { text: string, speaker: string } {
  const none = { text: '', speaker: '' }
  if (!scroller) return none
  const rows = scroller.querySelectorAll<HTMLElement>('[data-display-index]')
  let last: HTMLElement | null = null
  let lastIndex = -1
  for (const row of rows) {
    // A row nested in another came from message content (the sanitizer admits
    // `data-*` and `class`), so only the transcript's own wrappers count.
    if (!isTranscriptRow(row, scroller)) continue
    const index = Number(row.dataset.displayIndex)
    if (Number.isFinite(index) && index > lastIndex) { lastIndex = index; last = row }
  }
  if (!last) return none
  // Likewise a bubble or card inside a message is that message's content.
  const messages = [...last.querySelectorAll<HTMLElement>(MESSAGE_ELEMENTS)]
    .filter(m => !m.parentElement?.closest(MESSAGE_ELEMENTS))
  const message = messages[messages.length - 1]
  if (!message) return none
  const heading = message.closest('[data-role]')?.querySelector('h2.sr-only')
  const copy = message.cloneNode(true) as HTMLElement
  copy.querySelectorAll(NOT_SPOKEN).forEach(n => n.remove())
  // `textContent` has no image alternatives and no boundaries between table
  // cells, so a table would read as one run-on word. An image is read as its
  // alt text, cells are joined with commas and rows with newlines, as the
  // markdown fallback reads them.
  copy.querySelectorAll('img').forEach(img => img.replaceWith(img.getAttribute('alt') ? ` ${img.getAttribute('alt')} ` : ''))
  copy.querySelectorAll('tr').forEach(tr => {
    const cells = [...tr.children].filter(c => c instanceof HTMLTableCellElement)
    cells.slice(0, -1).forEach(c => c.append(', '))
    tr.append('\n')
  })
  // Every whitespace run but a newline becomes one space first, so the newline
  // pass below never backtracks across a long run (another person's text can
  // hold 100,000 ideographic spaces, and `\s*\n\s*` over that is quadratic).
  const text = (copy.textContent ?? '').replace(/[^\S\n]+/g, ' ').replace(/ ?\n ?/g, '\n').replace(/\n{2,}/g, '\n').trim()
  return { text, speaker: heading?.textContent?.trim() ?? '' }
}

export function TranscriptLiveRegion({ scrollerRef, announcement, tailSpeaker, tailMounted }: {
  scrollerRef: React.RefObject<HTMLElement | null>
  /** The text to speak, already worded; also the fallback for a tail row. */
  announcement: string
  /** The speaker when `announcement` is about the newest row; `null` otherwise. */
  tailSpeaker: string | null
  /** Whether the newest row is mounted (no unmounted rows below the window). */
  tailMounted: boolean
}) {
  // Starts empty: for a newest-row announcement the region must not show the
  // fallback first and then swap in the drawn text, or it would speak twice.
  const [text, setText] = useState('')
  const lastRef = useRef('')
  // Read when an announcement arrives, never as a dependency: a scroll that
  // mounts or drops the tail while text is held must not speak it again.
  const tailMountedRef = useRef(tailMounted)
  tailMountedRef.current = tailMounted
  useEffect(() => {
    if (!announcement || tailSpeaker === null || !tailMountedRef.current) {
      setText(announcement)
      lastRef.current = announcement
      return
    }
    // The row the announcement is about mounts in this commit or a frame later
    // (the virtualizer measures it first), so read it after a short delay.
    const timer = setTimeout(() => {
      const drawn = renderedTail(scrollerRef.current)
      const next = drawn.text
        ? i18nT('pages.chat.transcriptAnnouncement.spoken', { speaker: drawn.speaker || tailSpeaker, text: drawn.text })
        : announcement
      // A live region speaks a CHANGE; the same words again (a second identical
      // reply) get a trailing no-break space, which is not spoken.
      const changed = next === lastRef.current ? next + String.fromCharCode(0xa0) : next
      lastRef.current = changed
      setText(changed)
    }, TAIL_READ_DELAY_MS)
    return () => clearTimeout(timer)
  }, [announcement, tailSpeaker, scrollerRef])
  return <span className="sr-only" aria-live="polite" aria-atomic="true">{text}</span>
}
