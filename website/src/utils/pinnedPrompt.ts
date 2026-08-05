import type { DisplayItem } from '../pages/chat/types'

/**
 * Geometry + selection helpers for the pinned-prompt banner (the most recent
 * user prompt that has scrolled fully behind the chat fold, shown as a sticky
 * band under the session title).
 *
 * The hand-off is **bottom-edge driven**: a prompt scrolls with the transcript
 * until its bubble's BOTTOM edge reaches the bottom of the banner band, i.e.
 * until the row is entirely hidden behind the band; only then does it collapse
 * into the banner. It is then pushed out by the NEXT prompt as that prompt's top
 * border meets it (`computePinPush`) — a separate, earlier line, so a tall prompt
 * shows no banner at all while it is being read.
 *
 * Why the bottom edge and not the top (the original sticky-style rule): a prompt
 * taller than the band — an essay, a pasted stack trace — satisfies "top has
 * reached the fold" the instant it is sent, so it would collapse into a one-line
 * banner before the user could read it, and its still-laid-out (but hidden) row
 * left a prompt-sized hole above the response. Tracking the bottom edge means a
 * tall prompt stays fully readable and scrolls away line by line. For a
 * one-line prompt the two rules fire on the same pixel (its bubble height equals
 * the collapsed card height), so short-prompt behaviour is unchanged.
 *
 * The banner cannot be a real sticky element because the transcript is
 * virtualized — a row scrolled far above the window unmounts, so the sticky node
 * would vanish. Instead the banner is an overlay driven by the same math.
 */

/**
 * Vertical padding around the bubble inside a row (`py-1` on both the transcript
 * message row in ChatPage and the pinned band in PinnedPrompt). Single-sourced
 * here because the hand-off line is derived from it.
 */
export const ROW_PAD_Y = 4

/**
 * Height of the COLLAPSED banner card, used only until the real card has been
 * measured once (nothing is pinned on first load, so there is no card to read).
 * One line of `text-sm` (14px) at `leading-relaxed` (1.625 → 22.75px) plus the
 * paragraph's `my-1.5` (6+6) and the box's `py-1.5` (6+6) = 46.75px. A different
 * host font size only skews the very first hand-off of a session; every
 * subsequent one uses the measured height.
 */
export const DEFAULT_PINNED_CARD_H = 46.75

/**
 * Viewport Y of the hand-off line: the BOTTOM edge of the banner band. A prompt
 * pins once its row bottom has risen to or above this line (the row is then
 * completely covered by the band, so the swap is invisible), and un-pins the
 * moment it drops back below it.
 *
 * @param foldY         viewport Y of the fold sentinel = the band's top edge
 * @param collapsedCardH measured height of the collapsed banner card
 */
export function pinHandoffY(foldY: number, collapsedCardH: number): number {
  return foldY + ROW_PAD_Y * 2 + collapsedCardH
}

/** Only user-typed prompts pin. `nudge` opens a turn too but is machine-injected. */
function isPrompt(item: DisplayItem | undefined): boolean {
  return !!item && item.kind === 'single' && item.msg.role === 'user'
}

/**
 * Display index of the prompt that should be pinned, or -1 for none.
 *
 * Rows are laid out in order, so their bottom edges increase monotonically with
 * index: the rows already fully above the hand-off line are exactly the prefix
 * before `handoffIdx`. The pinned prompt is therefore the last prompt STRICTLY
 * before it — the row straddling the line is still readable in the transcript
 * and must not be swapped for the banner yet.
 *
 * @param items      the flattened display list
 * @param handoffIdx display index of the first row whose bottom is still below
 *                   the hand-off line (see `pinHandoffY`)
 */
export function findPinnedPromptIdx(items: DisplayItem[], handoffIdx: number): number {
  if (handoffIdx < 0) return -1
  const start = Math.min(handoffIdx - 1, items.length - 1)
  for (let i = start; i >= 0; i--) {
    if (isPrompt(items[i])) return i
  }
  return -1
}

/** Display index of the first prompt after `afterIdx`, or -1 if none. */
export function findNextPromptIdx(items: DisplayItem[], afterIdx: number): number {
  for (let i = Math.max(afterIdx + 1, 0); i < items.length; i++) {
    if (isPrompt(items[i])) return i
  }
  return -1
}

/**
 * How far (px) to translate the banner UP so the incoming prompt pushes it out.
 *
 * The banner's bottom edge tracks the incoming prompt's TOP edge exactly, so the
 * two never overlap: the push starts when the incoming top reaches the banner's
 * bottom (`gap === ROW_PAD_Y + bannerH`, the card's own bottom, since the card
 * sits ROW_PAD_Y below the fold) and completes when it reaches the fold
 * (`gap === 0`), by which point the card is entirely above the fold.
 *
 * The travel is therefore `ROW_PAD_Y + bannerH`, NOT `bannerH`: the card starts
 * ROW_PAD_Y below the fold, so a `bannerH` travel strands its last ROW_PAD_Y of
 * height inside the band. That was invisible while the push completed on the same
 * frame as the hand-off (the incoming card replaced it instantly), but once the
 * two lines separated for tall prompts it became a 4px strip of the outgoing
 * bubble's bottom edge parked over the incoming prompt for the whole no-banner
 * stretch — flickering in size with every sub-pixel scroll.
 *
 * Note this is a DIFFERENT line from the one that decides which prompt is pinned
 * (`pinHandoffY`, driven by the incoming prompt's BOTTOM edge), and deliberately
 * so. For a prompt taller than the band the two separate: the card is fully
 * pushed out while the prompt's top is still rising, and the prompt only takes
 * the pin later, once its bottom clears the band. The stretch between them —
 * where no banner is shown at all while the tall prompt is read — is intended:
 * the band would otherwise slide up across the prompt's own text, since by then
 * the only part of it beside the band is its last line. For a one-line prompt the
 * two lines coincide and the hand-off is instantaneous, as before.
 *
 * @param nextTop viewport-relative top of the incoming prompt row, or null when
 *                that row is not mounted (i.e. still far below the fold)
 */
/**
 * Total distance the banner must travel to leave the band COMPLETELY: its own
 * height plus the `ROW_PAD_Y` it sits below the fold. Once `computePinPush`
 * returns this, no part of the card is inside the band any more and ChatPage
 * drops the banner outright rather than rendering a fully-clipped one — a card
 * clipped to zero still leaves a 1-2px slice of its bottom edge under sub-pixel
 * rounding and browser zoom, parked over the incoming prompt for the whole
 * stretch while it is read.
 */
export function pinPushTravel(bannerH: number): number {
  return ROW_PAD_Y + bannerH
}

export function computePinPush(bannerH: number, foldY: number, nextTop: number | null): number {
  if (nextTop == null || bannerH <= 0) return 0
  const travel = pinPushTravel(bannerH)
  const gap = nextTop - foldY
  if (gap >= travel) return 0
  return Math.max(0, Math.min(travel, travel - gap))
}

/**
 * Lines of prompt text the COLLAPSED card shows before clamping.
 *
 * One line was the original choice and it loses too much: a long prompt is the
 * one most worth summarising, and a single clamped line of it is usually just its
 * opening clause. Three keeps the card small enough to sit under the title
 * without dominating the viewport, and it widens the range over which the card is
 * a pixel-exact copy of the bubble it replaces — every prompt up to three lines
 * now hands over with no size change at all, where before only a one-liner did.
 *
 * Consequence for the hand-off line: a taller card pushes `pinHandoffY` DOWN,
 * which makes the pin condition (`rowBottom <= handoffY`) EASIER to satisfy, so a
 * card growing after it mounts can never invalidate the pin that mounted it. The
 * coupling is monotone in the safe direction — see the test of the same name.
 */
export const PINNED_PREVIEW_LINES = 3

/**
 * Markdown image syntax. Shared by `promptPreview` (which removes it from the
 * text) and `promptImages` (which collects the sources), so the two can never
 * disagree about what counts as an image — the failure mode being a prompt whose
 * image is stripped from the text AND missed by the thumbnail pass, i.e. silently
 * lost. `g` is set; `matchAll` clones the regex and `replace` resets `lastIndex`
 * itself, so the shared instance carries no state between calls.
 */
const IMAGE_MD_RE = /!\[[^\]]*\]\(([^)]*)\)/g

/** Fenced code block. Shared so every pass agrees on where code starts and ends. */
const FENCE_RE = /```[\s\S]*?```/g

/**
 * Partition a prompt into fenced-code and prose segments.
 *
 * The image passes MUST agree on fences, and sharing `IMAGE_MD_RE` alone was not
 * enough to guarantee it: `promptPreview` folded fences away BEFORE looking for
 * images, while `promptImages`/`promptBody` ran on raw content. A prompt that
 * merely QUOTED image markdown inside a code fence therefore produced a phantom
 * thumbnail for an image it never attached, and had that line rewritten inside the
 * quoted code in the expanded view — i.e. the passes disagreed by ordering, not by
 * pattern. Routing all three through this one split makes the agreement structural.
 */
function splitFences(content: string): { fence: boolean; text: string }[] {
  const out: { fence: boolean; text: string }[] = []
  let last = 0
  for (const m of content.matchAll(FENCE_RE)) {
    const at = m.index ?? 0
    if (at > last) out.push({ fence: false, text: content.slice(last, at) })
    out.push({ fence: true, text: m[0] })
    last = at + m[0].length
  }
  if (last < content.length) out.push({ fence: false, text: content.slice(last) })
  return out
}

/**
 * Flatten a prompt to plain text for the collapsed banner.
 *
 * Images are removed from the TEXT because their markdown (`![alt](/very/long/
 * path.png)`) is noise at a glance — but they are not discarded: `promptImages`
 * pulls them out separately and the card renders them as thumbnails. Dropping
 * them here and rendering nothing was the bug that made an image-only prompt pin
 * as a completely empty card.
 *
 * `[attached_file N] /abs/path` collapses to the basename and fenced code becomes
 * an ellipsis.
 */
export function promptPreview(content: string): string {
  return content
    .replace(FENCE_RE, ' … ')
    .replace(IMAGE_MD_RE, ' ')
    .replace(/\[attached_file \d+\]\s*(\S+)/g, (_m, p: string) => p.split('/').pop() || '')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * The prompt as authored, minus the image markdown — what the EXPANDED card
 * shows.
 *
 * Unlike `promptPreview` this keeps line structure: expanded is the "read it
 * properly" view, so paragraphs and hard breaks matter. Only the image syntax is
 * removed, because the card renders those images as a strip directly above this
 * text; leaving the markdown in printed the source of an image the user is already
 * looking at (`![screenshot](/tmp/a.png)` as literal text under the thumbnail).
 *
 * Blank lines left behind by the removal are collapsed so an image on its own line
 * does not open a gap, and a prompt that was ONLY images yields an empty string —
 * the strip is then the whole card.
 */
export function promptBody(content: string): string {
  // Fence-aware: quoted code keeps its image syntax verbatim (it is the code the
  // user is showing us); only real attachments are removed. The blank-line rule
  // runs PER non-fence segment rather than over the joined string — doing it after
  // the join re-applied IMAGE_MD_RE to everything, fences included, which deleted
  // the very quoted lines this split exists to protect.
  return splitFences(content)
    .map(seg => {
      if (seg.fence) return seg.text
      return seg.text
        .split('\n')
        .map(line => ({ line, stripped: line.replace(IMAGE_MD_RE, '') }))
        // A line that HELD an image and is now blank contributed nothing but that
        // image: drop it, so an image on its own line leaves no hole. A line that
        // was blank to begin with is authored spacing and is kept verbatim — this
        // is the read-it-properly view, so the user's paragraph breaks survive.
        .filter(({ line, stripped }) => stripped.trim() !== '' || line.trim() === '')
        .map(({ stripped }) => stripped)
        .join('\n')
    })
    .join('')
    .trim()
}

/**
 * Image sources referenced by a prompt, in document order, deduplicated.
 *
 * Returned raw (as authored) — resolving a local path to a fetchable URL is the
 * renderer's job, and `PinnedPrompt` defers to the same `/api/file-raw` mapping
 * `MarkdownRenderer`'s `img` handler uses, so a thumbnail and the bubble's own
 * copy of the image always resolve identically.
 *
 * Empty sources are dropped: `![alt]()` is legal markdown that would otherwise
 * render a broken thumbnail.
 */
export function promptImages(content: string): string[] {
  const out: string[] = []
  for (const seg of splitFences(content)) {
    // Skip fences: an image merely QUOTED in a code block is not an attachment,
    // and thumbnailing it invents an image the prompt never carried.
    if (seg.fence) continue
    for (const m of seg.text.matchAll(IMAGE_MD_RE)) {
      const src = (m[1] || '').trim()
      if (src && !out.includes(src)) out.push(src)
    }
  }
  return out
}

/**
 * Fetchable URL for a thumbnail source, mirroring `MarkdownRenderer`'s `img`
 * handler: a local path goes through the gateway's `/api/file-raw`, anything
 * already absolute (http/https/data) is passed straight through.
 *
 * Deliberately narrower than the renderer's version — it has no BasePathCtx to
 * resolve a relative path against, and a pinned prompt is user-authored content
 * with no base document, so a bare relative path is treated as local and left to
 * the API to reject rather than being resolved against nothing.
 */
/**
 * Gateway endpoint that serves a local file's bytes. Hoisted to a const because
 * the i18n lint applies its shape exclusions (which exempt path-shaped literals)
 * to a literal node, but reports a literal used as an operand of `+` as the whole
 * binary expression — so an inline `'/api/…' + encodeURIComponent(x)` lands in the
 * untranslated-copy ratchet for what is a URL.
 */
const FILE_RAW_PATH_PREFIX = '/api/file-raw?path='

export function pinnedImageUrl(src: string): string {
  return /^(?:https?:|data:|blob:)/i.test(src)
    ? src
    : FILE_RAW_PATH_PREFIX + encodeURIComponent(src)
}
