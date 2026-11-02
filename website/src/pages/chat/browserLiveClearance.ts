/**
 * Keep the browser card's header clear of the transcript's top overlay.
 *
 * The chat transcript scrolls UNDER an absolute overlay: the title row and the
 * pinned-prompt banner (`ChatTitleOverlay`, marked `data-transcript-top-overlay`,
 * and the banner's own `data-testid="pinned-prompt"`). When the person takes
 * over, the card grows and takes focus, and its header (who is in control,
 * the address, Hand back) can end up under that overlay. This measures how far
 * the header's top edge sits under the overlay's lowest edge and scrolls the
 * card's scroll container down by exactly that much, plus a small gap.
 *
 * Measured from layout, never from the banner's internals, so the banner stays
 * untouched apart from two marker attributes: one on the overlay root, one on the
 * banner's band.
 *
 * While the person holds the browser the pinned-prompt banner also steps aside
 * (`holdPinnedPromptAside`): it pins the prompt that scrolled away, which during
 * a takeover is the prompt above this very card, and it would otherwise sit on
 * the card's header whenever the card and what follows it fill the view.
 */

export const TOP_OVERLAY_ATTR = 'data-transcript-top-overlay'

/** Space kept between the overlay's lower edge and the card's top, in px. */
export const CLEARANCE_GAP_PX = 8

/** The overlay's elements (the title row's root and the pinned-prompt card). */
export function topOverlays(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>(`[${TOP_OVERLAY_ATTR}], [data-testid="pinned-prompt"]`)]
}

/** The overlay's lowest edge on screen, or null when there is none. */
function overlayBottom(): number | null {
  let bottom = -Infinity
  for (const el of topOverlays()) {
    // A banner set aside (visibility hidden) keeps its box but covers nothing.
    if (getComputedStyle(el).visibility === 'hidden') continue
    const r = el.getBoundingClientRect()
    if (r.height > 0 && r.bottom > bottom) bottom = r.bottom
  }
  return Number.isFinite(bottom) ? bottom : null
}

/** How far (px) the card's top sits under the transcript's top overlay; 0 when clear. */
export function overlayOverlap(card: HTMLElement, gap = CLEARANCE_GAP_PX): number {
  const bottom = overlayBottom()
  if (bottom === null) return 0
  return Math.max(0, Math.ceil(bottom + gap - card.getBoundingClientRect().top))
}

/**
 * How much of the scroller's visible height the overlay covers from its top, in
 * px: what a card that must fit in view can NOT use. 0 with no overlay.
 */
export function overlayInset(scroller: HTMLElement, gap = CLEARANCE_GAP_PX): number {
  const bottom = overlayBottom()
  if (bottom === null) return 0
  return Math.max(0, Math.ceil(bottom + gap - scroller.getBoundingClientRect().top))
}

function scrollParent(el: HTMLElement): HTMLElement | null {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const oy = getComputedStyle(p).overflowY
    if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight) return p
  }
  return null
}

/** Scroll the card down from under the overlay; returns how many px it moved it. */
export function clearTopOverlay(card: HTMLElement): number {
  const overlap = overlayOverlap(card)
  if (overlap <= 0) return 0
  const scroller = scrollParent(card)
  if (!scroller) return 0
  scroller.scrollTop = Math.max(0, scroller.scrollTop - overlap)
  return overlap
}

/** Set on `<html>` while the banner is aside; the rule that hides the band is in index.css. */
export const ASIDE_ATTR = 'data-browser-live-takeover'
let asideHolders = 0

/**
 * Set the pinned-prompt banner aside while a card is in the person's control;
 * returns the release. Counted, so two cards in control (two panes) both hold it.
 * The whole band goes (card, mask fill and fade), by the band's one marker
 * attribute (`data-pinned-prompt-band`); it comes back unchanged on release.
 */
export function holdPinnedPromptAside(): () => void {
  asideHolders += 1
  document.documentElement.setAttribute(ASIDE_ATTR, '')
  let released = false
  return () => {
    if (released) return
    released = true
    asideHolders = Math.max(0, asideHolders - 1)
    if (asideHolders === 0) document.documentElement.removeAttribute(ASIDE_ATTR)
  }
}

/** Whether the banner is set aside now (tests and screenshots read this). */
export function pinnedPromptAside(): boolean {
  return document.documentElement.hasAttribute(ASIDE_ATTR)
}
