import type { ChatMessage } from '../types'
import { stripMarkersForDisplay } from '../app-sdk/protocol/optionMarker'
import { stripKeepVisibleMarker } from '../app-sdk/protocol/keepVisibleMarker'

// `<mcwidget>...</mcwidget>` bodies render as a sandboxed iframe (WidgetFrame).
// Their visible text lives in a separate document the in-chat highlighter's
// TreeWalker cannot reach, and the rest is non-visible HTML/CSS — so searching
// raw widget content only ever yields phantom (un-highlightable) matches.
// Excluding the whole block is intentionally conservative: we only strip
// content that is NOT highlightable body text. Highlightable content (fenced
// code, prose, collapsed-but-mounted reasoning) is deliberately left in so
// search never misses something the user can actually see highlighted.
const MCWIDGET_RE = /<mcwidget\b[^>]*>[\s\S]*?<\/mcwidget>/gi

/**
 * The text that is actually rendered (and highlightable) in the chat body for a
 * message. Used by both the search scan (occurrence counting) and the results
 * panel (snippet building) so the result list, the "N results" count, and the
 * highlighted marks all stay consistent. Searching raw content would surface
 * "phantom" matches (inside OPTIONS buttons or widget iframes) the user can
 * never see highlighted.
 *
 * `stripMarkersForDisplay` removes BOTH marker kinds. Stripping only the content
 * marker left action-marker text searchable, which is precisely the phantom match
 * the paragraph above forbids: a hit is counted and reported, then the highlighter's
 * TreeWalker finds nothing to mark because the marker never rendered as body text.
 *
 * It is the DISPLAY helper rather than `stripOptionMarkers` so this index cannot drift
 * from what the transcript actually shows. The one row that helper retains a marker for
 * — a message whose only content is an action marker — does render it as body text, so
 * excluding it would invert the rule above: a hit the user can see but never find.
 *
 * ORDER MATTERS, and it is markers FIRST: that helper's "nothing left to read" test has to
 * run on the same string the transcript renders, and the transcript passes the WHOLE
 * message to `parseOptions` (`AssistantMessage.tsx` — `parseOptions(effectiveContent)`),
 * widget included. Removing the widget first made a marker beside one look like the row's
 * only content, so the index retained its raw text while the transcript still had the
 * widget to render and stripped it — a phantom hit, the exact failure above.
 */
export function searchableText(m: ChatMessage): string {
  if (m.role === 'assistant' || m.role === 'streaming') {
    // stripKeepVisibleMarker: the marker is an HTML comment the renderer never
    // shows (#7948), so a search hit inside it would be a phantom match.
    return stripKeepVisibleMarker(stripMarkersForDisplay(m.content).replace(MCWIDGET_RE, '')).trimEnd()
  }
  return m.content
}

// Memo cache keyed by the message object. The search scan recomputes on every
// (debounced) keystroke and SearchResultsList recomputes again for snippets, so
// without this the two regex passes run ~once per message per keystroke on large
// sessions. A WeakMap auto-evicts when messages are dropped (no unbounded
// growth); the stored `content` guards against in-place mutation during
// streaming, where the same object's content changes.
const _memo = new WeakMap<ChatMessage, { content: string; text: string }>()

/**
 * Memoized {@link searchableText}. Returns the same value, cached per message
 * object and invalidated when that message's `content` changes. Use on hot
 * paths (the keystroke-debounced match loop and snippet building).
 */
export function searchableTextMemo(m: ChatMessage): string {
  const hit = _memo.get(m)
  if (hit && hit.content === m.content) return hit.text
  const text = searchableText(m)
  _memo.set(m, { content: m.content, text })
  return text
}
