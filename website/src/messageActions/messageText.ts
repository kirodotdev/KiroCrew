/**
 * The text a message action is handed: the message as the reader sees it.
 *
 * By default the VISIBLE text of the message's own content in the bubble, read
 * from the page at the moment of the click (`utils/visibleText`): what the chat
 * shows, by construction, so a link gives its label and never its address, an
 * image or diagram gives nothing, a formula gives its TeX once, and a code block
 * gives its code without line numbers or button labels. Only the content roots
 * the renderers mark `data-message-body` are read (the rendered markdown, a
 * user's plain-text segment); a quoted card, a seed label, knowledge chips,
 * attachment cards and steer chips around them are not. The app still gets a
 * string, never a DOM node. Markdown is a separate, approved format and is the
 * source. Either way the text is cut to the action's `maxChars` in UTF-16 units,
 * never inside a surrogate pair.
 *
 * @module messageActions/messageText
 */
import { visibleText } from '../utils/visibleText'

/** Marks a renderer's content root inside a message bubble. */
export const MESSAGE_BODY_ATTR = 'data-message-body'

/**
 * The visible text of a message bubble's content, or `null` when it cannot be
 * read: the bubble is gone from the page (scrolled out of a virtualized list, or
 * closed), it has no content root, or the content shows nothing. A `null`
 * refuses the plain format rather than guessing.
 */
export function readMessageText(bubble: Element | null | undefined): string | null {
  if (!bubble || !bubble.isConnected) return null
  const roots = Array.from(bubble.querySelectorAll(`[${MESSAGE_BODY_ATTR}]`))
    // Outermost roots only: a nested one is already inside its parent's text.
    .filter((el) => {
      const outer = el.parentElement?.closest(`[${MESSAGE_BODY_ATTR}]`)
      return !outer || !bubble.contains(outer)
    })
  const text = roots.map(visibleText).filter(Boolean).join('\n')
  return text ? text : null
}

/** `text` cut to at most `max` UTF-16 units, never splitting a surrogate pair. */
export function truncateUnits(text: string, max: number): { text: string; truncated: boolean } {
  if (text.length <= max) return { text, truncated: false }
  let end = max
  const code = text.charCodeAt(end - 1)
  if (code >= 0xd800 && code <= 0xdbff) end -= 1
  return { text: text.slice(0, end), truncated: true }
}

/**
 * The text for one invocation, in the action's approved format: the markdown
 * source, or the visible text read at the click. `null` when the plain format
 * was asked for and the bubble could not be read.
 */
export function messageTextFor(
  source: { markdown: string; text: string | null },
  format: 'plain' | 'markdown',
  maxChars: number,
): { text: string; truncated: boolean } | null {
  if (format === 'markdown') return truncateUnits(source.markdown, maxChars)
  return source.text === null ? null : truncateUnits(source.text, maxChars)
}
