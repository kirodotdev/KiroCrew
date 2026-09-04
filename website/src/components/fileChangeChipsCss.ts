/**
 * Injected stylesheet text for the chat file-change chips, plus the two layout
 * numbers and the animation duration the rules interpolate.
 *
 * Separate module because these are `unsafeCSS` templates handed to the CSS
 * parser -- selectors, lengths, keyframes -- never words anyone reads. Keeping
 * them out of the component is what lets the component stay fully covered by
 * the i18n gate (see this path in `eslint.i18n.config.js`).
 */
import { PIERRE_COMPACT_HEADER_CSS, PIERRE_WRAP_NO_HSCROLL_CSS, PIERRE_SEPARATOR_BG_CSS } from '../pierre/config'

/* ── Expanded row: the closed state is a lightweight outer-tree header, so a
 *   transcript can hydrate many changed files without loading Pierre or
 *   parsing their diffs. Opening a row swaps in one Pierre surface; the
 *   collapse animation keeps that surface mounted for one final frame, then
 *   returns to the lightweight header.
 *
 *   Pierre's OPEN header layout, left to right: the chevron (header PREFIX slot), Pierre's
 *   change icon + filename, then the metadata slot (Open + diffstat cells) and
 *   Pierre's own ±counts last. Pierre's shadow template puts the counts BEFORE
 *   the metadata slot, so the counts are kept rightmost by an `order` flip on
 *   the slotted content in index.css — the metadata row is a flex container
 *   and slotted nodes are light DOM, so ordering them from outside works.
 *
 *   Open, the diff scrolls INSIDE the row: the code element is the scroller
 *   (bounded height), so the header sits outside the scroll area and a long
 *   diff cannot wall off the transcript. */
export const ROW_BODY_MAX_H = 376
export const ROW_ANIM_MS = 180
/* Width of Pierre's metadata group, pinned so the diffstat indicator starts at
 * the SAME x on every row. Two things move it otherwise, and the second is the
 * one that actually bites: the counts are 1-3 digits wide, and Pierre omits a
 * count span ENTIRELY when its side is zero (`createMetadataElement` pushes
 * `-N` only when deletions > 0) — so an additions-only file renders one span
 * instead of two and the whole group narrows. Fixing the span widths cannot
 * help with a span that does not exist; a fixed-width group with the indicator
 * pinned left and the counts pinned right can. Sized for two 4ch counts, the
 * 46px indicator and the gaps between them. */
export const ROW_META_W = 124

/* Injected per shadow root via Pierre's `unsafeCSS` (its sanctioned hook —
 * `@layer unsafe`, which beats the library's own `@layer base` regardless of
 * specificity). Outer CSS cannot reach any of these: they live in the shadow
 * root. Three jobs:
 *
 *  1. The header sits on --bg-elevated so the card reads as one raised object
 *     while the code keeps the chat canvas.
 *  2. The ±count spans get a fixed width, so the metadata group stops changing
 *     width between rows — that jitter is what made the diffstat cells look
 *     like they were starting from a different place on every row.
 *  3. The SCROLLER is the code element, not the row: a scrollbar on the row
 *     would run the full height and sit beside the header too, and the header
 *     is naturally always reachable once it is outside the scroll area. */
export const ROW_CSS_BASE = `
${PIERRE_COMPACT_HEADER_CSS}
${PIERRE_WRAP_NO_HSCROLL_CSS}
${PIERRE_SEPARATOR_BG_CSS}
/* Half-way between the chat canvas and --bg-elevated: the full elevated tone
 * reads as prominently as the composer, which pulls the eye to the header
 * instead of the change it labels. */
[data-diffs-header]{background-color:color-mix(in srgb,var(--bg-elevated) 50%,var(--bg))}
[data-diffs-header]{padding-inline:10px}
[data-deletions-count],[data-additions-count]{display:inline-block;min-width:4ch;text-align:right}
[data-metadata]{flex:0 0 ${ROW_META_W}px;justify-content:space-between}
pre{max-height:${ROW_BODY_MAX_H}px;overflow-y:auto}
`
export const ROW_CSS_CLICKABLE_TITLE = `
/* The filename is the open-file control. Pierre owns that element, so the
 * affordance has to be injected: pointer + accent on hover, matching how links
 * read elsewhere in chat. Scoped to the title so the icon and counts beside it
 * stay inert. */
[data-title]{cursor:pointer}
[data-title]:hover{color:var(--accent)}
`


/* A fresh mount cannot transition (there is no start state), so the reveal is a
 * keyframe. Collapse runs the same animation reversed while the body is held
 * mounted for one animation, then Pierre collapses it away for real. */
export const ROW_CSS_OPEN = `${ROW_CSS_BASE}
@keyframes fccReveal{from{max-height:0}to{max-height:${ROW_BODY_MAX_H}px}}
pre{animation:fccReveal ${ROW_ANIM_MS}ms ease}
`
export const ROW_CSS_CLOSING = `${ROW_CSS_BASE}
@keyframes fccHide{from{max-height:${ROW_BODY_MAX_H}px}to{max-height:0}}
pre{animation:fccHide ${ROW_ANIM_MS}ms ease;animation-fill-mode:forwards;overflow:hidden}
`
