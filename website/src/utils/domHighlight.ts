import { escapeRegex } from './highlightText'

/**
 * Transcript search highlights, painted through the CSS Custom Highlight API
 * (`CSS.highlights` + `Range`) rather than by wrapping matches in `<mark>`.
 *
 * The message bubbles are React-reconciled (react-markdown). Splitting a
 * React-owned text node to insert an element, or merging the pieces back with
 * `normalize()`, leaves React's fiber pointing at a node that is not where React
 * put it: the next commit that deletes that node throws NotFoundError ("Failed
 * to execute 'removeChild' on 'Node': The node to be removed is not a child of
 * this node"), which MessageErrorBoundary surfaces as `message_render`, and a
 * commit that rewrites it updates a detached node so the visible text goes
 * stale. A streaming message re-parses on every token and a settled one still
 * restructures (glow spans dropping out, link unfurls, path chips resolving), so
 * any find-bar term that matches the bubble hits one of those commits.
 *
 * A `Range` lives outside the DOM: registering it paints the characters without
 * touching a node, and when React does replace the node the range collapses
 * harmlessly until the owner re-runs the walk (AssistantMessage does so from a
 * MutationObserver). MarkdownPanel's preview find uses the same API for the same
 * reason. Styling lives in `index.css` under `::highlight(mc-chat-match)` and
 * `::highlight(mc-chat-current)`: background-color only, which every engine
 * with the API paints; the current match is the stronger fill.
 *
 * Two highlights are shared by every bubble on the page, so each caller's
 * ranges are tracked per element and withdrawn by {@link clearSearchHighlights}
 * — the owner MUST call it on unmount, or a virtualized row that scrolls away
 * keeps its detached subtree alive through the ranges that point into it.
 *
 * The ranges are built and tracked whether or not the browser can paint them:
 * without the API (Firefox before 140, Safari before 17.2) matches paint
 * nothing, but {@link getCurrentSearchRange} still names the current one, so
 * Enter / Shift+Enter still center it. Nothing here can throw or mutate.
 */

/** Registry names. Distinct from MarkdownPanel's `mc-find*` so a transcript search
 *  and a preview find that are open at once cannot overwrite each other. */
export const SEARCH_HL_MATCH = 'mc-chat-match'
export const SEARCH_HL_CURRENT = 'mc-chat-current'

interface HighlightApi {
  match: Highlight
  current: Highlight
}

let shared: HighlightApi | null = null

/** Ranges each bubble has registered, so they can be withdrawn without a walk. */
const owned = new WeakMap<HTMLElement, Range[]>()
let currentRange: Range | null = null
let currentOwner: HTMLElement | null = null

/** The page-wide highlights, registered on first use; null where the API is absent. */
function highlightApi(): HighlightApi | null {
  if (typeof Highlight === 'undefined' || typeof CSS === 'undefined' || !CSS.highlights) return null
  if (!shared) {
    shared = { match: new Highlight(), current: new Highlight() }
    // The current match paints over a plain match should the two ever share a
    // character (they do not by construction; this keeps the outcome defined).
    shared.current.priority = 1
    CSS.highlights.set(SEARCH_HL_MATCH, shared.match)
    CSS.highlights.set(SEARCH_HL_CURRENT, shared.current)
  }
  return shared
}

/**
 * Find every occurrence of `term` inside `el`, paint it where the browser can,
 * and record the current one for {@link getCurrentSearchRange}.
 * @param currentOcc — 0-based index of the occurrence painted as the current
 *                     match. -1 paints all occurrences as plain matches.
 */
export function applySearchHighlights(
  el: HTMLElement,
  term: string,
  caseSensitive: boolean,
  currentOcc: number,
): void {
  clearSearchHighlights(el)
  if (!term) return
  const api = highlightApi()

  const re = new RegExp(escapeRegex(term), caseSensitive ? 'g' : 'gi')
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  const ranges: Range[] = []
  let occIdx = 0
  let node: Node | null
  while ((node = walker.nextNode())) {
    const text = (node as Text).data
    if (!text) continue
    re.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(text))) {
      const r = document.createRange()
      r.setStart(node, m.index)
      r.setEnd(node, m.index + m[0].length)
      ranges.push(r)
      if (occIdx === currentOcc) {
        api?.current.add(r)
        currentRange = r
        currentOwner = el
      } else {
        api?.match.add(r)
      }
      occIdx++
    }
  }
  if (ranges.length) owned.set(el, ranges)
}

/** Withdraw every range `el` registered. Touches no DOM node. */
export function clearSearchHighlights(el: HTMLElement): void {
  const ranges = owned.get(el)
  if (currentOwner === el) {
    currentRange = null
    currentOwner = null
  }
  if (!ranges) return
  owned.delete(el)
  if (!shared) return
  for (const r of ranges) {
    shared.match.delete(r)
    shared.current.delete(r)
  }
}

/**
 * The range painted as the current match, or null when there is none, when its
 * text node has left the document, or when a rewrite collapsed it. A caller
 * that centers the match (see `scrollCurrentMatchIntoView`) polls this, so a
 * stale range reads as "not mounted yet" rather than as a position.
 */
export function getCurrentSearchRange(): Range | null {
  const r = currentRange
  if (!r || r.collapsed || !r.startContainer.isConnected) return null
  return r
}
