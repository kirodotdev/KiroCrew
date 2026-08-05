/**
 * Shared layout constants — single source of truth for magic numbers.
 *
 * IMPORTANT: These constants can be used in JS logic and inline `style` props,
 * but NEVER interpolated into Tailwind class strings (e.g. `w-[${X}px]`).
 * Tailwind purges classes at build time and cannot resolve JS variables.
 * For Tailwind classes, use hardcoded literals: `w-[260px]`, `grid-cols-[220px_...]`.
 */
export const LAYOUT = {
  NAV_WIDTH: 220,
  NAV_COLLAPSED_WIDTH: 56,
  CHAT_SIDEBAR_WIDTH: 260,
  MAX_MESSAGE_WIDTH: 820,
  AGENT_LIST_HEIGHT: 420,
  LOG_LINE_CAP: 500,
  TOPBAR_HEIGHT: 52,
} as const
