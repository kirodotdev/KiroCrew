/**
 * The class recipes a conversation-list panel is made of, shared by the
 * Sessions sidebar (`pages/ChatSidebar.tsx`) and the Crew Members roster
 * (`pages/members/MembersPage.tsx`) so the two lists read as one surface:
 * same card, same header line, same row box, same type scale.
 *
 * They live here as whole class strings rather than as values copied between
 * the two files because copied values drift silently — the roster once carried
 * its own header padding, row weight and line-heights, and in kiro-light its own
 * background, none of which anything flagged. `src/test/listShellParity.test.ts`
 * pins both pages to these constants.
 */

/** The card itself. `sidebar` is the theme-pack hook (website/docs/theming-contract.md,
 *  "Stable hooks") and `sidebar-inner` the kiro-light shell hook (index.css) that
 *  steps the card back to `--panel` on the white canvas — both must lead the
 *  string; `src/test/kiroLightShellHooks.test.ts` anchors on that. */
export const LIST_SHELL_CLS = 'sidebar sidebar-inner bg-bg-elevated border border-border rounded-xl shadow-sm'

/** Header row: title and actions centred on one line 23px from the panel top
 *  (1px card border + mt-0.5, then centred in the 40px row) — the shared
 *  control baseline the nav rail header and chat title row also sit on. px-2
 *  puts a 28px action button 9px from the card's right edge, matching its 9px
 *  gap to the top. */
export const LIST_HEADER_CLS = 'flex justify-between items-center px-2 mt-0.5 h-10'

/** The panel title inside that header. `sessions-panel-title` is the class the
 *  kiro themes retint (index.css). */
export const LIST_TITLE_CLS = 'sessions-panel-title text-sm font-semibold text-text-strong tracking-[.04em] truncate'

/** The scrolling list body. */
export const LIST_BODY_CLS = 'flex-1 min-h-0 overflow-y-auto scrollbar-none p-2'

/** A row's box: the rounded hover/selection surface. */
export const ROW_BOX_CLS = 'pl-3.5 pr-3 py-2 rounded-md'
/** A row at rest, and the selected row. */
export const ROW_IDLE_CLS = 'text-muted hover:text-text hover:bg-bg-hover'
export const ROW_ACTIVE_CLS = 'text-text-strong bg-accent-subtle'

/*
 * Row type scale — three boxes, 12 / 20 / 16, chosen so the secondary line
 * yields to the headline instead of competing with it (11/13/12 sat within 2px
 * of each other and CJK glyphs fill their em box). The boxes need not match
 * each other: each status marker centres on the line it leads, not on the row.
 *
 * A third surface tracks these by copied value: the Notes app's left rail
 * (`apps/md-notebook/constants.ts`, `RAIL_TYPE`) is styled inline and cannot
 * import class strings. Change a size here and update `RAIL_TYPE` in the same
 * commit, or that rail silently diverges.
 */
export const ROW_META_CLS = 'text-[10px] leading-[12px]'
export const ROW_TITLE_CLS = 'text-[13px] leading-[20px]'
export const ROW_STATUS_CLS = 'text-[11px] leading-[16px]'
