/**
 * The per-element typography both document surfaces in the side panel share:
 * `ELEMENT_OVERRIDES` (markdown) and `OfficeBlockList` (a `.docx`'s structured
 * preview). A heading in a `.docx` and a heading in a `.md` are the same thing
 * to a reader, so they read one owner here; restyling one restyles the other.
 *
 * Each constant is the class string a surface renders verbatim, or the shared
 * base it appends its own layout tokens to. What a surface appends is its own:
 * a markdown header never wraps mid-label, while an Office cell keeps the
 * newlines of a cell that holds several paragraphs.
 *
 * Vertical rhythm (#27000) uses one unit, u = a quarter of the body size
 * (3.5px at 14px), and an order where things that belong together sit closer:
 *
 *   list item / list under its lead-in   1.5u
 *   paragraph, heading -> its first block 3u
 *   new section (any block -> heading)   7u
 *
 * Blocks carry a top margin and, for headings, a bottom one; adjacent margins
 * collapse, so a heading's 3u bottom and a list's 1.5u top yield 3u, and a
 * paragraph (no bottom margin) followed by a list yields 1.5u. Every value is
 * in `em` of the element's OWN font size, so a heading's margins are its
 * section gap divided by its size ratio to the 14px body (20/18/16/14/14/13px
 * for h1-h6), and the whole rhythm follows the chat's font-size setting, which
 * scales headings by the same ratios (`message-font-size.css`).
 */

/** `h1`–`h6`, indexed by level − 1. */
export const MD_HEADING_CLS = [
  'text-xl font-semibold leading-[1.35] mt-[1.225em] mb-[0.525em] text-text-strong',
  'text-lg font-semibold leading-[1.35] mt-[1.3611em] mb-[0.5833em] text-text-strong',
  'text-base font-semibold leading-[1.35] mt-[1.5313em] mb-[0.6563em] text-text-strong',
  'text-sm font-semibold leading-[1.35] mt-[1.75em] mb-[0.75em] text-text-strong',
  'text-sm font-medium leading-[1.35] mt-[1.75em] mb-[0.75em] text-text-strong',
  'text-[13px] font-medium leading-[1.35] mt-[1.8846em] mb-[0.8077em] text-muted',
] as const

/** The renderer root. The first block of a message carries no top margin:
 *  without this its 3u (or a heading's 7u) would collapse through the root
 *  and push the message down from its own top edge. Blocks sit one wrapper
 *  deep (rehypeStableRootKeys' display:contents div), two with source
 *  positions on (the `data-block-start` group). */
export const MD_FLOW_ROOT_CLS = '[&>:first-child>:first-child]:mt-0 [&>:first-child>:first-child>:first-child]:mt-0'

/** A paragraph: 3u above, nothing below, so a list under it sits at 1.5u. */
export const MD_PARAGRAPH_CLS = 'mt-[0.75em] mb-0 leading-normal'

/** Shared list rhythm: 1.5u above the list and between its items. */
const MD_LIST_RHYTHM_CLS = 'mt-[0.375em] mb-0 [&>li+li]:mt-[0.375em]'

/** A bulleted list. */
export const MD_UL_CLS = `list-disc pl-8 ${MD_LIST_RHYTHM_CLS} marker:text-muted`

/** A numbered list without its marker token, which a typed `<ol>` replaces
 *  with an inline `list-style-type`. */
export const MD_OL_BASE_CLS = `pl-8 ${MD_LIST_RHYTHM_CLS} marker:text-muted`

/** A numbered list with the decimal marker. */
export const MD_OL_CLS = `list-decimal ${MD_OL_BASE_CLS}`

/** A task list (`- [ ]` items), either `<ul>` or `<ol>`. */
export const MD_TASK_LIST_CLS = `list-none pl-4 ${MD_LIST_RHYTHM_CLS}`

/** A list item (not a task item). */
export const MD_LIST_ITEM_CLS = 'text-sm leading-normal'

/** A table header cell, before the surface's wrapping rule. No fill: the
 *  header reads as the strongest row through weight and colour, and the first
 *  column starts flush with the prose around the table (#27001). Padding is
 *  in `em` of the 13px header text: 2u above and below, 4u to the right. */
export const MD_TABLE_HEAD_CELL_CLS = 'text-left text-text-strong text-[13px] font-semibold pl-0 pr-[1.0769em] py-[0.5385em] border-b border-border-strong'

/** A table body cell, before the surface's wrapping rule: 2.5u above and
 *  below, 4u to the right, flush left like the header. */
export const MD_TABLE_CELL_CLS = 'pl-0 pr-[1em] py-[0.625em] border-b border-border text-sm leading-normal'

/** Bold and italic inline runs. */
export const MD_STRONG_CLS = 'font-semibold text-text-strong'
export const MD_EM_CLS = 'italic'
