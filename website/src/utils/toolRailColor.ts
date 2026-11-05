/**
 * The colour of a tool row's detail rail: the left bar of the expanded
 * Input/Output panel, matched to the row's status icon. One mapping for every
 * surface that draws a tool row -- the main chat's `ToolCallLine` and the SDK's
 * store-free `ToolCallPill` -- so the two cannot drift.
 *
 * Only the colour is shared. Each host derives its own flags, because the
 * hosts know different things: the main chat folds edit runs and only treats a
 * row as rejected or auto-denied once the whole run is done, while the embed
 * has no runs. Pass the flags as the host means them; the precedence here is
 * rejected > auto-denied > done > awaiting approval > running.
 *
 * Returned as a `color-mix()` string at 70% rather than a Tailwind opacity
 * class: the project's Tailwind config does not compile `border-{color}/N`
 * opacity variants for theme colours.
 *
 * Pure and import-free on purpose: the SDK embed runs with no Redux store and
 * no router, so this module must never reach for either.
 */
export interface ToolRailFlags {
  isRejected: boolean
  isAutoDenied: boolean
  isDone: boolean
  hasPendingPerm: boolean
}

export function toolRailColor({ isRejected, isAutoDenied, isDone, hasPendingPerm }: ToolRailFlags): string {
  const base = isRejected
    ? 'var(--danger)'
    : isAutoDenied
      ? 'var(--warn)'
      : isDone
        ? 'var(--ok)'
        : hasPendingPerm
          ? 'var(--warn)'
          : 'var(--accent)'
  return `color-mix(in srgb, ${base} 70%, transparent)`
}
