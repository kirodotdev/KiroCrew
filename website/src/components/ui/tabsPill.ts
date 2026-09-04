/**
 * The pill switcher's class recipe, in ONE place.
 *
 * Two components render this control and they are not interchangeable:
 *
 *   ui/tabs.tsx  — Radix Tabs. Use when each tab owns its own panel, so the
 *                  trigger ⇄ panel `aria-controls` pair is real.
 *   Tablist.tsx  — a tablist and nothing else. Use when the body below is ONE
 *                  shared subtree parameterised by the active tab, where Radix's
 *                  unconditional `aria-controls` would point at nothing.
 *
 * They must look identical, because a user reading a page cannot see which of the
 * two accessibility shapes is underneath. A shared recipe is what makes that
 * structural rather than a promise: there is no second copy to drift.
 *
 * `SegmentedControl` deliberately keeps its own copy of these metrics — it is a
 * FILTER, a different control with the same skin, and folding it in here would
 * couple two independent decisions.
 */

/**
 * The recessed track the segments sit in.
 *
 * `flex w-fit`, NOT `inline-flex`, even though both hug their content: the i18n
 * render scanner groups an element's direct children into maximal INLINE runs, and
 * anything whose computed `display` starts with `inline` is swallowed into its
 * parent's run as one lump of text. An `inline-flex` rail therefore reads as
 * several catalog keys glued into a single unit ("Servers" + "Sharing assessment"),
 * which is the `fragment/multi-unit` finding. Block-level display makes the parent
 * flush at the rail, so each tab is graded on its own.
 *
 * `max-w-full overflow-x-auto` because the segments are `whitespace-nowrap` and
 * this track does not measure its parent the way `SegmentedControl` does (which
 * collapses to icons, then to a dropdown). Without it a rail wider than a narrow
 * pane — a 288px Developer pane at 320px, where a locale like Russian or
 * Portuguese runs far longer than the English — pushes its last tab off-screen
 * with no way to reach it. Scrolling keeps every tab reachable; the sliding
 * indicator lives inside a segment, so it scrolls with the rail rather than
 * detaching from it.
 */
export const TABS_TRACK_CLASS =
  'flex w-fit max-w-full items-center gap-0.5 overflow-x-auto rounded-lg border border-border bg-bg-elevated p-0.5'

/**
 * One segment. No border in the base: the sliding indicator carries it, so a
 * selection never shifts the label by a pixel, and the box metrics stay identical
 * to `SegmentedControl`'s.
 *
 * `isolate` makes the segment its own stacking context so the indicator can sit
 * at `-z-10` — BEHIND the label, but still in front of the track, because a
 * stacking context paints negative-z children above its own background. That is
 * what lets the label render as a direct child: wrapping it in a positioned span
 * instead would give every tab's text one shared source location, which the
 * i18n render scanner reads as several catalog keys glued into one unit.
 *
 * Focus uses the `.focus-ring` utility rather than the global `:focus-visible`
 * outline: that outline is `outline-offset: 2px`, which on a pill inside a 2px
 * track paints a box straddling the track's own border.
 */
export const TABS_SEGMENT_CLASS = [
  'focus-ring group/tab relative isolate flex cursor-pointer items-center gap-1.5 whitespace-nowrap',
  'rounded-md px-2.5 py-1.5 text-[12px] font-medium',
  'text-muted transition-colors hover:text-text',
].join(' ')

/** Applied to the SELECTED segment, on top of `TABS_SEGMENT_CLASS`. */
export const TABS_SEGMENT_ACTIVE_CLASS = 'text-accent'

/** Applied to a segment the surface knows about and cannot serve yet. */
export const TABS_SEGMENT_DISABLED_CLASS = 'cursor-not-allowed text-muted/40 hover:text-muted/40'

/** The `aria-disabled:` form, for the same scanner reason as above. */
export const TABS_SEGMENT_DISABLED_ARIA_CLASS = [
  'aria-disabled:cursor-not-allowed',
  'aria-disabled:text-muted/40',
  'aria-disabled:hover:text-muted/40',
].join(' ')

/** The pill that slides between segments. Sits behind the label via `-z-10`
 *  inside the segment's own stacking context. */
export const TABS_INDICATOR_CLASS = 'absolute inset-0 -z-10 rounded-md border border-border bg-card shadow-sm'

/** Spring the indicator travels on. Matches `SegmentedControl`'s. */
export const TABS_INDICATOR_SPRING = { type: 'spring', stiffness: 500, damping: 35 } as const

/**
 * Trailing count, shared part only. The SELECTED colour is deliberately NOT here:
 * the two components learn which segment is selected by different means — Radix
 * from its own `data-state`, `Tablist` from a prop — so each spells its own
 * variant at the point of use. There is nothing common to drift.
 */
export const TABS_COUNT_BASE_CLASS = 'text-[11px]'

/**
 * The row a page's tab rail sits in: the rail, then a rule, then the content.
 *
 * The rule is the only thing distinguishing a NAVIGATION rail from a
 * `SegmentedControl` FILTER now that both wear this pill, so it belongs to the
 * pattern rather than to each page's own judgement — the System page is why. It
 * stacks a plane rail directly above the sessions table's own `Group by` filter,
 * and dressing the two identically is what previously made that page read as two
 * pill rows with no hierarchy between them. A rail that skips the rule leaves the
 * reader inferring from position alone which pill changes screens.
 *
 * Compose, do not replace: a rail inside a flex column adds its own `shrink-0`.
 */
export const TABS_RAIL_ROW_CLASS = 'mb-4 border-b border-border pb-3'
