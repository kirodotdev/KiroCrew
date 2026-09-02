import type { DisplayItem } from '../pages/chat/types'
import { TURN_OPENER_ROLES } from '../pages/chat/groupDisplayItems'
import { mdImageDestToPath } from './fileTokens'
import { type PasteBlock, expandAll } from './pasteTokens'

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

/**
 * Rows that can take the pin: the ones that OPEN a turn.
 *
 * Derived from `TURN_OPENER_ROLES` rather than restated, because the two lists
 * disagreeing is the defect this exists to prevent. A nudge and a subagent
 * completion are machine-injected, but each IS the thing that started the turn
 * being read, and a session made almost entirely of them (a babysit loop, a
 * workflow fan-out) otherwise offers no pinnable row cycle after cycle — the walk
 * upward skips every one and lands on the human's last typed message, dozens of
 * turns and tens of thousands of pixels away.
 */
function isPrompt(item: DisplayItem | undefined): boolean {
  return !!item && item.kind === 'single' && TURN_OPENER_ROLES.has(item.msg.role)
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
 * Whether a transcript row must be HIDDEN because the banner stands in for it.
 *
 * Pure and here rather than inline in a style attribute because it is the one rule
 * in the feature whose failure DELETES a message: the row is hidden on the promise
 * that the banner shows the same content in the same place, and any state that
 * breaks that promise while leaving this true makes the message unreachable in
 * both places at once. `minimized` is checked first for exactly that reason — a
 * chip stands in for nothing.
 *
 * Identity beats position: the pin's index is computed in a scroll rAF and a
 * streaming append can shift the list before the row renders, which hid the wrong
 * row. Match on the message timestamp when the pin has one, falling back to the
 * index only for a message carrying none, and pass no timestamp for a grouped row.
 */
export function pinHidesRow(
  pin: { ts?: string; idx: number } | null,
  minimized: boolean,
  row: { ts?: string; idx: number },
): boolean {
  if (minimized || !pin) return false
  return pin.ts != null ? row.ts === pin.ts : pin.idx === row.idx
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
 * The walk deliberately consumes MACHINE turn openers too (nudge and subagent
 * rows — `isPrompt` derives from `TURN_OPENER_ROLES`), not only consecutive
 * user rows like a steer following its prompt. The mechanism-backed case is a
 * fan-out whose completions drain back to back: each is a turn opener and none
 * of them carries a reply of its own, so they lay out as one run of consecutive
 * opener rows. Consecutive
 * nudge rows arise only when nudged turns persist no reply (an errored or
 * cancelled cycle — a normal cycle interposes its tool/assistant rows). In
 * both shapes each row is pinnable, and each belongs to the same contextual
 * block as the row directly above the run: jumping to any member lands at the
 * block's top, where the exchange reads in order — stopping mid-run would
 * drop the reader between two machine rows with the context that explains
 * them still hidden above.
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
}

/** What the scroll recompute knows before any derivation is done. */
export interface PinnedPromptInput {
  idx: number
  ts?: string
  /** Stored (collapsed) prompt content — the identity the derivation keys on. */
  raw: string
  pastes: PasteBlock[]
  /** Compact label a machine-authored row shows instead of its payload. */
  machineLabel: string | null
  /** Body such a row reveals when expanded, when it differs from `raw`. */
  machineBody?: string
  push: number
  bannerH: number
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
  const { idx, ts, raw, pastes, machineLabel, machineBody, push, bannerH } = input
  const sameMsg = prev !== null && prev.idx === idx && prev.raw === raw && prev.ts === ts
  if (sameMsg && prev.push === push && prev.bannerH === bannerH) return prev
  if (sameMsg) return { ...prev, push, bannerH }
  const derived = machineLabel === null ? derivePinnedPromptText(raw, pastes) : null
  const text = machineLabel ?? derived?.text ?? ''
  const full = machineBody ?? derived?.body ?? raw
  return {
    idx,
    ts,
    text,
    raw,
    full,
    images: derived?.images ?? [],
    // By COMPARISON, not by being machine-authored: a short multiline paste never
    // clamps, so this flag is the only thing that can reach its body.
    bodyBeyondPreview: full !== text,
    push,
    bannerH,
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
