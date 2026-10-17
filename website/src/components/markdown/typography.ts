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
 */

/** `h1`–`h6`, indexed by level − 1. */
export const MD_HEADING_CLS = [
  'text-xl font-bold mt-4 mb-2 text-text-strong',
  'text-lg font-bold mt-3 mb-2 text-text-strong',
  'text-base font-semibold mt-3 mb-1.5 text-text-strong',
  'text-sm font-semibold mt-2 mb-1 text-text-strong',
  'text-sm font-medium mt-2 mb-1 text-text-strong',
  'text-[13px] font-medium mt-2 mb-1 text-muted',
] as const

/** A bulleted list. */
export const MD_UL_CLS = 'list-disc pl-8 my-2 space-y-1 marker:text-muted'

/** A numbered list without its marker token, which a typed `<ol>` replaces
 *  with an inline `list-style-type`. */
export const MD_OL_BASE_CLS = 'pl-8 my-2 space-y-1 marker:text-muted'

/** A numbered list with the decimal marker. */
export const MD_OL_CLS = `list-decimal ${MD_OL_BASE_CLS}`

/** A list item (not a task item). */
export const MD_LIST_ITEM_CLS = 'text-sm leading-relaxed'

/** A table header cell, before the surface's wrapping rule. */
export const MD_TABLE_HEAD_CELL_CLS = 'text-left text-muted text-[13px] font-medium px-3 py-2 border-b border-border bg-bg-elevated'

/** A table body cell, before the surface's wrapping rule. */
export const MD_TABLE_CELL_CLS = 'px-3 py-2 border-b border-border text-sm'

/** Bold and italic inline runs. */
export const MD_STRONG_CLS = 'font-semibold text-text-strong'
export const MD_EM_CLS = 'italic'
