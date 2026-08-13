/**
 * How to find a session row's ONE status marker in the DOM, by its own classes.
 *
 * Shared by the two harnesses that measure it, and deliberately shape-agnostic:
 * `capture-session-status-marker.mjs` captures a before/after pair, and
 * `capture-session-row-grid.mjs` has a `GRID_BASELINE=1` mode, so BOTH run the same
 * code against two different DOMs — the marker leading the secondary line (current)
 * and the marker inside an absolute left gutter (pre-fix). Walking a fixed child
 * index finds the wrong element in one of the two and crashes the measurement
 * instead of reporting the baseline it exists to capture.
 *
 * The list is explicit rather than a broad `svg` sweep because a row holds other
 * glyphs that are NOT the status marker: the flat-view folder chip, PR/issue
 * provider logos, the channel-provenance mark, the pin. Each entry below is a
 * marker one status branch owns (`ChatSidebar.tsx`, the `rowState` resolver), plus
 * the unread dot, which is a `span` and has no lucide class at all.
 */
export const MARKER_SELECTORS = [
  'svg.animate-spin',                            // running — the spinner
  'svg.lucide-shield-check',                     // pending approval
  'svg.lucide-message-circle-question-mark',     // needs input
  'svg.lucide-goal',                             // goal loop
  'svg.lucide-bot',                              // sub-agents (running or awaiting approval)
  'svg.lucide-workflow',                         // dynamic workflow run
  'span.rounded-full',                           // unread — "your turn"
]
