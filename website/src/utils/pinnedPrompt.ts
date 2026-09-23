import type { DisplayItem } from '../pages/chat/types'
import { isSubagentCompletionMessage } from '../pages/chat/subagentCompletion'
import { mdImageDestToPath } from './fileTokens'
import { type PasteBlock, expandAll } from './pasteTokens'

/**
 * Geometry + selection helpers for the pinned-prompt banner (the most recent
 * user prompt that has scrolled up to the chat fold, shown as a sticky band
 * under the session title).
 *
 * The hand-off is **top-edge driven**: a prompt scrolls with the transcript
 * until its bubble's TOP edge reaches the card's own resting top, and hands over
 * there. That line is where the card sits, so the bubble stops travelling at the
 * exact pixel the card occupies — which is the whole point. It is then pushed out
 * by the NEXT prompt as that prompt's top border meets it (`computePinPush`).
 *
 * Why the top edge and not the bottom (the rule this replaced): the bottom-edge
 * rule waited until the row was entirely behind the band, so any prompt TALLER
 * than the card kept scrolling after it had passed the card's position, went out
 * of sight behind the header, and the card then appeared back down at the fold —
 * content jumping down the screen after having scrolled past its own resting
 * place (measured at 78px for a four-line prompt against a one-line card). The
 * top edge cannot do that: `snapBackPx` is 0 by construction.
 *
 * The hand-off line therefore does NOT depend on the card's height, and must not:
 * the clamp (`PINNED_RESTING_LINES`) is a presentation choice that may change, and
 * a hand-off derived from it moves the swap point with it. A prompt no taller than
 * the clamp hands over with no size change at all; a taller one FOLDS in place at
 * the line, animated by the card's own height morph (see PinnedPrompt), instead of
 * being swapped after a journey. Both fall out of the same rule at any clamp value.
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
 * Viewport Y of the hand-off line: the card's own resting TOP edge. A prompt pins
 * once its row top has reached this line, and un-pins the moment it drops back
 * below it.
 *
 * The row and the band both put `ROW_PAD_Y` above their bubble, so a row whose TOP
 * is on the fold has its bubble on the card's top: handing over there is a swap
 * between two boxes that start at the same pixel, whatever either one's height is.
 *
 * Takes no card height ON PURPOSE. The previous rule added the card's measured
 * height to this line, which coupled the swap point to the clamp: change
 * `PINNED_RESTING_LINES` and the hand-off moved, and any prompt taller than the
 * clamp overshot the line by exactly the difference. Keeping the clamp out of the
 * line is what makes the fix hold at every clamp value.
 *
 * @param foldY viewport Y of the fold sentinel = the band's top edge
 */
export function pinHandoffY(foldY: number): number {
  return foldY
}

/**
 * Rows that can take the pin: what the HUMAN typed.
 *
 * Deliberately NARROWER than `TURN_OPENER_ROLES`. A nudge and a subagent
 * completion open turns too (the grouping keeps treating them that way), but they
 * are machine-injected: a banner that quotes "Auto-nudge · cycle 36" over the
 * transcript tells the reader nothing they asked, and in a babysit loop it
 * re-pins on EVERY cycle, so scrolling through the session shows a fresh machine
 * row taking the band every few screens. The banner exists to answer "what did I
 * ask that this is a reply to", and only a row the user authored can answer it —
 * so the walk upward skips every machine opener and lands on the last typed
 * prompt, however many cycles ago that was. The distance is the honest answer:
 * nothing closer was the user's.
 *
 * A STEER (`meta.steer`, set by the `steer_push` echo) IS admitted, on the same
 * grounds that admit any other typed row: the user wrote it, and inside the reply
 * that followed it, it is the most recent thing they asked. This deliberately
 * differs from the turn-BOUNDARY scans that share its shape —
 * `isTurnBoundaryUser` and the `lastUserIdx` walk in `store/chatSlice.ts`, the
 * turn-head walk in `app-sdk/turnPolicyBlock.ts` — all of which must skip a steer
 * because they answer "where does this turn BEGIN", and a row injected into a
 * running turn cannot end that scan. The banner answers "what did I last ask",
 * so it takes the opposite answer. The row that OPENED a steered turn is not
 * lost: it is the head of the steer's own prompt run, so it is where clicking the
 * banner lands (`jumpAnchorIdx`) or one step further up the chain.
 *
 * A subagent completion in OLDER scrollback was persisted under role `user`
 * (before the `subagent` role existed) and IS excluded, by SHAPE rather than by
 * role: the same completion-event parser the transcript card uses recognises it.
 */
function isPrompt(item: DisplayItem | undefined): boolean {
  if (!item || item.kind !== 'single') return false
  const { msg } = item
  return msg.role === 'user' && !isSubagentCompletionMessage(msg)
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
 * Display index of the row the pinned-prompt jump should scroll to when the
 * user asks for `target`.
 *
 * Normally that is `target` itself. But when the rows immediately BEFORE the
 * target are also prompts (turn openers, per `isPrompt`), the jump anchors at
 * the FIRST prompt of that consecutive run — the walk finds the top of the
 * contextual block the target belongs to.
 *
 * Why not land on `target` directly: putting it at the jump chrome leaves the
 * prompt above it straddling the hand-off line — it cannot pin (its bottom is
 * still below the line) while its own top edge has already pushed the fallback
 * banner fully out — so the banner unmounts and the jump chain dies on a
 * landing the scan treats as a transient. Anchoring at the head of the run
 * puts a non-prompt row (or the top of the list) on the line instead, so the
 * previous turn's banner survives and the chain continues.
 *
 * The walk consumes only rows `isPrompt` admits — consecutive rows the USER
 * typed: a steer sent before the turn produced any output, a double-send, a
 * prompt typed while the previous one was still queued. Those are exactly the
 * rows that can pin and push, so they are the only ones that can straddle. A
 * steer sent before the turn produced any output lies directly on its opener, so
 * the walk anchors there and clicking that pinned steer scrolls to the prompt
 * that started the work; a steer that arrived after some output has a reply row
 * above it and is its own anchor. Machine openers (nudge and
 * subagent rows) are not prompts here, so a run of them above the target is an
 * ordinary non-prompt gap and the walk stops at the target: the previous user
 * prompt's banner then survives the landing exactly as it would over any other
 * reply row. A nudge is a one-line self-labelled system row and a subagent
 * completion a self-labelled headline card, so a landing beside either reads on
 * its own, with the banner above it naming the prompt the whole block answers.
 *
 * Walking up lengthens the jump. The virtualizer's near/far decision
 * (`mountIndex` in useVirtualChat) compares the anchor's jump window against
 * the COMMITTED window with `NEAR_JUMP_OVERSCAN_MULT` overscan windows of
 * slack (24 rows for the transcript, which passes `overscan: 6`) — a budget
 * shared with the distance the jump already covers, so the walk consumes
 * whatever slack a near jump has left over. In the common case (the pinned
 * prompt is the previous turn) that leaves the glide untouched; a jump
 * already sitting at the band's edge can be tipped onto the far path by the
 * walk, and that is the right outcome there — the gap is unmounted spacer,
 * and a glide across it would scrub blank.
 *
 * For a target with a non-prompt row above it this returns `target` unchanged.
 */
export function jumpAnchorIdx(items: DisplayItem[], target: number): number {
  let anchor = target
  while (anchor > 0 && isPrompt(items[anchor - 1])) anchor -= 1
  return anchor
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

/**
 * Height the pinned card should be on this frame — the progressive fold.
 *
 * The card's top is fixed at `foldY + ROW_PAD_Y`. Its bottom should sit on the
 * pinned row's bottom edge, because that is where the reply begins: match them and
 * there is no gap between the card and the reply, at any prompt height. So the
 * height wanted is simply the distance from the card's top to the row's bottom.
 *
 * Bounded at both ends, and each bound is load-bearing:
 *   - never above `bubbleH`, so a freshly pinned prompt is a pixel-exact stand-in
 *     for the bubble rather than a taller box that pushes the reply down;
 *   - never below `restingH`, because the card cannot show less than its clamp. Past
 *     that point the row's slot is smaller than the card and the reply slides under
 *     it, which is the same one-line overlap the band already has at rest.
 *
 * @param rowBottomFromFold pinned row's bottom edge, relative to the fold line
 * @param restingH          settled height of the clamped card
 * @param bubbleH           height of the bubble the card stands in for
 */
export function computeLiveCardH(
  rowBottomFromFold: number,
  restingH: number,
  bubbleH: number,
): number {
  // A bubble smaller than the clamp (a one-word prompt) has nothing to fold: the
  // card is already its resting size and the max() below would otherwise stretch
  // it past the bubble it is copying.
  const ceiling = Math.max(restingH, bubbleH)
  const wanted = rowBottomFromFold - ROW_PAD_Y
  return Math.min(ceiling, Math.max(restingH, wanted))
}

export function computePinPush(bannerH: number, foldY: number, nextTop: number | null): number {
  if (nextTop == null || bannerH <= 0) return 0
  const travel = pinPushTravel(bannerH)
  const gap = nextTop - foldY
  if (gap >= travel) return 0
  return Math.max(0, Math.min(travel, travel - gap))
}

/**
 * Lines of prompt text the card shows AT REST — one.
 *
 * The card sits over the top of whatever reply the reader is scrolling through,
 * so its resting height is space taken from that reply. Three resting lines
 * (the earlier value) cost ~13% of a phone viewport on every long turn, and the
 * reporter of #4984 named it as the one piece of chrome that actively gets in
 * the way of reading. One line is enough to answer "what did I ask" at a
 * glance; the fuller preview is one hover away (`PINNED_PREVIEW_LINES`), and
 * the whole prompt one click away (the chevron).
 *
 * Consequence for the hand-off line: the card's settled resting height is what
 * `pinHandoffY` is derived from, and a SHORTER card moves that line UP, so a
 * prompt hands over sooner. The pin condition (`rowBottom <= handoffY`) is
 * evaluated against this settled height only — the hover peek and the full
 * expansion grow the live card but never re-report a collapsed height — so a
 * card growing after it mounts can never invalidate the pin that mounted it.
 * See the test of the same name.
 */
export const PINNED_RESTING_LINES = 1

/**
 * Lines of prompt text the card shows while the pointer is over it or a control
 * inside it has keyboard focus — the PEEK.
 *
 * A single clamped line of a long prompt is usually just its opening clause;
 * three lines is the amount that reliably carries the ask. Hover is the right
 * trigger because it costs the reader nothing when they are not interested: the
 * card grows only while they point at it and shrinks back the moment they leave.
 * Touch has no hover, so on touch the chevron is the way to more than one line.
 */
export const PINNED_PREVIEW_LINES = 3

/**
 * Markdown image syntax. Shared by `promptPreview` (which removes it from the
 * text) and `promptImages` (which collects the sources), so the two can never
 * disagree about what counts as an image — the failure mode being a prompt whose
 * image is stripped from the text AND missed by the thumbnail pass, i.e. silently
 * lost. `g` is set; `matchAll` clones the regex and `replace` resets `lastIndex`
 * itself, so the shared instance carries no state between calls.
 *
 * The destination has two CommonMark shapes, mirrored from mdImageDest
 * (fileTokens.ts): a plain run up to the first `)`, or an angle-bracket form
 * `<…>` that may contain spaces, parentheses, and backslash-escaped `\<` `\>`
 * `\\` — the `<…>` alternative must come first, or a wrapped destination
 * containing `)` (e.g. `</tmp/screenshot (1).png>`) is cut at that paren.
 */
const IMAGE_MD_RE = /!\[[^\]]*\]\((<(?:\\[\\<>]|[^<>\\])*>|[^)]*)\)/g

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
      // mdImageDest wraps whitespace/special-char destinations in CommonMark's
      // `<…>` form with `\`, `<`, `>` backslash-escaped. This extractor reads
      // the RAW markdown (micromark never sees it), so resolve the on-disk
      // path with the shared wrap-aware inverse: producer-wrapped `<…>`
      // destinations are unescaped and percent-decoded; unwrapped legacy
      // destinations are preserved verbatim (issue #3497).
      const src = mdImageDestToPath((m[1] || '').trim())
      if (src && !out.includes(src)) out.push(src)
    }
  }
  return out
}

/**
 * Per-block character budget applied when a pinned prompt's `[ Paste #N · M
 * lines ]` tokens are substituted for the text they stand for.
 *
 * Unbounded substitution is not an option here. The store deliberately keeps a
 * sent prompt's content in its COLLAPSED, token-bearing form (`recollapsePastes`
 * in pasteTokens) precisely so nothing downstream measures or lays out hundreds
 * of KB, and this module's consumer re-derives once per animation frame of a
 * scroll. A cap keeps both properties: enough text to fill the three-line
 * collapsed card and the scrollable expanded strip from real content, far short
 * of the sizes that froze the tab. The rest stays one click away — the card's
 * body IS a jump-to-turn button, and the bubble it jumps to has the paste in
 * full behind its own chip.
 */
export const PINNED_PASTE_HEAD_CHARS = 12000

/**
 * Substitute every `[ Paste #N · M lines ]` token in `content` for a head-capped
 * copy of its block's text, optionally passing that text through `mapBlock`.
 *
 * Ranges come from `findTokenRanges` — the same locator the bubble, the composer
 * highlight layer and the copy handler use — so the pinned card can never
 * disagree with them about which token belongs to which block. The splice walks
 * right-to-left so each write leaves the earlier ranges' offsets valid.
 *
 * A truncated block ends in ` …` so the card never implies the paste stopped
 * where the cap did.
 */
export function expandPastesCapped(
  content: string,
  blocks: PasteBlock[],
  mapBlock?: (text: string) => string,
): string {
  // Safe to delegate: `findTokenRanges` pairs a token to a block by `seq`, and
  // the spread preserves it, so rewriting content cannot move a range.
  return expandAll(content, blocks.map(b => {
    const capped = b.content.length > PINNED_PASTE_HEAD_CHARS
      ? b.content.slice(0, PINNED_PASTE_HEAD_CHARS) + ' …'
      : b.content
    return { ...b, content: mapBlock ? mapBlock(capped) : capped }
  }))
}

/** Everything the pinned card renders, derived from one prompt in one pass. */
export interface PinnedPromptText {
  /** Flattened, clamp-ready preview for the COLLAPSED card. */
  text: string
  /** Line-preserving body for the EXPANDED card. */
  body: string
  /** Image sources to thumbnail. */
  images: string[]
}

/** Collapse every whitespace run to a single space, as `promptPreview` does. */
function flattenWhitespace(s: string): string {
  return s.replace(/\s+/g, ' ').trim()
}

/**
 * Derive all three pinned-card values from a prompt's stored content and blocks.
 *
 * The store holds a sent prompt COLLAPSED, so reading `msg.content` straight
 * gave the card the literal `[ Paste #N ]` token — the empty-card failure images
 * already have an exemption for, and the placeholder the copy handler rejects as
 * "worthless on the other end" (UserMessage).
 *
 * Substitution happens AFTER the three prose passes, never before: they rewrite
 * markdown, and a paste is verbatim text the user is SHOWING us, so running them
 * over it would thumbnail an image the prompt never attached and delete the
 * pasted line that spelled it. The token holds no markdown and no newline, so it
 * survives all three untouched and substituting into their output exempts the
 * paste from them exactly. With no blocks this is the previous behaviour.
 */
export function derivePinnedPromptText(content: string, blocks: PasteBlock[]): PinnedPromptText {
  // Computed from the ORIGINAL content on purpose: an image inside a paste is
  // pasted text, not an attachment.
  const images = promptImages(content)
  if (!blocks.length) return { text: promptPreview(content), body: promptBody(content), images }
  return {
    text: expandPastesCapped(promptPreview(content), blocks, flattenWhitespace),
    body: expandPastesCapped(promptBody(content), blocks),
    images,
  }
}

/** The pinned banner's complete state, owned by the transcript page. */
export interface PinnedPromptState {
  idx: number
  ts?: string
  text: string
  raw: string
  full: string
  images: string[]
  bodyBeyondPreview: boolean
  push: number
  bannerH: number
  /**
   * Height the card should be RIGHT NOW, in px — the progressive fold.
   *
   * The card stands in for a row whose slot is still laid out (the row is hidden,
   * not removed), so a card SHORTER than that slot leaves a blank gap between
   * itself and the reply below. That gap is the size of the difference: measured
   * at 794px in a 700px viewport for a 30-line prompt against a one-line clamp.
   * Tracking the slot's remaining height removes the gap by construction — the
   * card is exactly as tall as the part of the row still above the reply, and it
   * shrinks line by line as the row scrolls away until it reaches its clamp.
   *
   * Computed from the ROW, never from the card (see computeLiveCardH). A height
   * derived from measuring the card would close a loop: the card's height feeds
   * `onCollapsedHeight`, which feeds `pinHandoffY` and `pinPushTravel`, which move
   * the geometry that decides the height. The resting height keeps that reporting
   * role alone.
   */
  liveH?: number
}

/** What the scroll recompute knows before any derivation is done. */
export interface PinnedPromptInput {
  idx: number
  ts?: string
  /** Stored (collapsed) prompt content — the identity the derivation keys on. */
  raw: string
  pastes: PasteBlock[]
  push: number
  bannerH: number
  /** See `PinnedPromptState.liveH`. Recomputed every scroll frame. */
  liveH?: number
}

/**
 * Next banner state, or `prev` itself when nothing a reader can see has moved.
 *
 * Derivation lives HERE rather than ahead of the call because the caller runs
 * once per animation frame of a scroll: on a frame that moved only the push
 * geometry the second branch carries the already-derived text forward, so the
 * three regex walks happen once per pinned message instead of once per frame.
 * That is what the cache this replaced was for.
 *
 * Identity is `(idx, raw, ts)` — the message — so two prompts that collapse to
 * the same token text cannot share a derivation the way a content-shape key let
 * them.
 */
export function nextPinnedPromptState(
  prev: PinnedPromptState | null,
  input: PinnedPromptInput,
): PinnedPromptState {
  const { idx, ts, raw, pastes, push, bannerH, liveH } = input
  const sameMsg = prev !== null && prev.idx === idx && prev.raw === raw && prev.ts === ts
  if (sameMsg && prev.push === push && prev.bannerH === bannerH && prev.liveH === liveH) return prev
  // `liveH` DOES move every frame — that is the fold. It is carried on the
  // same-message path for exactly that reason, unlike `push`/`bannerH` which only
  // change when the geometry does.
  if (sameMsg) return { ...prev, push, bannerH, liveH }
  const { text, body: full, images } = derivePinnedPromptText(raw, pastes)
  return {
    idx,
    ts,
    text,
    raw,
    full,
    images,
    // By COMPARISON: a short multiline paste never clamps, so this flag is the
    // only thing that can reach its body.
    bodyBeyondPreview: full !== text,
    push,
    bannerH,
    liveH,
  }
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
  // Sources arrive as on-disk paths (promptImages resolves the producer's
  // wrapped form via mdImageDestToPath), so encode them into the query as-is —
  // decoding here would corrupt a legacy path containing a literal `%XX`.
  return /^(?:https?:|data:|blob:)/i.test(src)
    ? src
    : FILE_RAW_PATH_PREFIX + encodeURIComponent(src)
}
