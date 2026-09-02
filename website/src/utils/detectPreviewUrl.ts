import type { ChatMessage } from '../types'

/**
 * Detect the URL the Web Preview tab should load, from the session's chat.
 *
 * Two signals:
 *  1. **Explicit marker** — a hidden HTML comment the agent emits when it starts
 *     a dev server: `<!-- kirocrew:preview url="http://127.0.0.1:8080" -->`.
 *     rehypeRaw drops HTML comments from the rendered message, so it's invisible
 *     to the user. Precise — it never fires on an unrelated URL.
 *  2. **Heuristic fallback** — a localhost/loopback dev-server URL mentioned in
 *     an assistant message (so it still works when the agent reports "serving at
 *     http://localhost:5173" without emitting the marker).
 *
 * Only assistant messages are scanned (the user's own pasted URLs shouldn't
 * hijack the preview). Selection is **chronological, newest message first**:
 * within a single message a marker beats a bare URL, but a newer message's
 * signal always wins over an older one — so a restarted server that reports a
 * new port in prose is NOT overridden by a stale marker further up the thread.
 * The caller treats `source: 'marker'` as explicit (auto-open) and
 * `source: 'heuristic'` as an offer (pre-fill only). Returns null if none.
 */
export interface PreviewUrlHit {
  url: string
  source: 'marker' | 'heuristic'
}

// `<!-- kirocrew:preview url="..." -->` — tolerant of surrounding whitespace.
const MARKER_RE = /<!--\s*kirocrew:preview\s+url="([^"]+)"\s*-->/gi
// Loopback dev-server URLs only (localhost / 127.0.0.1 / 0.0.0.0 / [::1]),
// optional port + path. Excludes quotes/brackets/whitespace from the path so a
// trailing `)` or `"` in prose isn't swallowed.
const LOCALHOST_URL_RE =
  /https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d{1,5})?(?:\/[^\s)"'<>]*)?/gi

/** Last capture-group-`group` match of `re` in `text`, or null. */
function lastMatch(re: RegExp, text: string, group: number): string | null {
  re.lastIndex = 0
  let m: RegExpExecArray | null
  let last: string | null = null
  while ((m = re.exec(text)) !== null) last = m[group]
  return last
}

export function detectPreviewUrl(messages: ChatMessage[]): PreviewUrlHit | null {
  // Newest assistant message first. Within a message the explicit marker wins
  // over an incidental URL; the first message (scanning back) with any signal
  // decides — so a newer prose URL isn't shadowed by an older marker.
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (msg.role !== 'assistant') continue
    const content = msg.content || ''
    const marker = lastMatch(MARKER_RE, content, 1)
    if (marker) return { url: marker, source: 'marker' }
    const loopback = lastMatch(LOCALHOST_URL_RE, content, 0)
    if (loopback) return { url: loopback, source: 'heuristic' }
  }
  return null
}

/** What a detected preview signal should do. `open=true` → open the tab and
 *  live-load; `open=false` → pre-fill only (never auto-open, never fire a GET). */
export interface PreviewFeed {
  url: string
  open: boolean
}

/**
 * Decide what to do with a detected signal, given whether the session already
 * has a preview target. An explicit **marker** always opens + loads (the agent
 * asked for it). A **heuristic** localhost URL is only an OFFER: it pre-fills
 * when nothing is set yet and NEVER auto-opens or loads — closing the drive-by
 * request vector where any localhost URL echoed by the model would auto-navigate
 * a local GET. Returns null when there's nothing to do.
 */
export function previewFeedDecision(
  hit: PreviewUrlHit | null,
  hasExistingTarget: boolean,
): PreviewFeed | null {
  if (!hit) return null
  if (hit.source === 'marker') return { url: hit.url, open: true }
  if (hasExistingTarget) return null
  return { url: hit.url, open: false }
}
