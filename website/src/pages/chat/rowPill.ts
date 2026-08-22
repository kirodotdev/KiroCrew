/**
 * Shared geometry for a transcript row's collapsible header pill.
 *
 * ToolCallLine (tool rows) and ThinkingBlock (reasoning rows) are siblings in
 * a turn and must read as one component family — same label size, same inner
 * padding, same icon gap, same left edge. Each component still owns its
 * display mode (inline-flex vs a full-width flex while a live preview line is
 * streaming), colors, and focus treatment; what lives here is the geometry
 * that must never diverge between them. The regression test imports these
 * constants too, which turns its token pinning into an equality guarantee:
 * restyling the pill means editing THIS string, and both rows plus the test
 * move together.
 *
 * (app-sdk/messageRenderers.tsx carries a third, historically separate copy;
 * folding it in is part of the shared-header-primitive follow-up, not this
 * module's job — the app-sdk deliberately avoids importing from pages/chat.)
 */

/** Wrapper around the pill button (and any trailing chips). The -ml-2 cancels
 *  the button's px-2 so the leading icon lands on the message column's text
 *  edge (x=0). It must stay on the wrapper, NOT the button — a negative margin
 *  on the button itself changes its shrink-to-fit width and wraps the label. */
export const ROW_PILL_WRAPPER_CLASS = 'max-w-full min-w-0 -ml-2'

/** The pill button's shared geometry: 13px label on a 20px line, px-2 py-0.5
 *  rounded-md box, gap-2 between the 12px leading icon and the label. */
export const ROW_PILL_BUTTON_CLASS =
  'items-start gap-2 min-w-0 max-w-full text-[13px] px-2 py-0.5 rounded-md transition-all text-left'
