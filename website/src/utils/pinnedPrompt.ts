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
 * Flatten a prompt to one line of plain text for the collapsed banner.
 * Attachment/image markdown carries no meaning at a glance, so images are
 * dropped, `[attached_file N] /abs/path` collapses to the basename, and fenced
 * code becomes an ellipsis.
 */
export function promptPreview(content: string): string {
  return content
    .replace(/```[\s\S]*?```/g, ' … ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[attached_file \d+\]\s*(\S+)/g, (_m, p: string) => p.split('/').pop() || '')
    .replace(/\s+/g, ' ')
    .trim()
}
