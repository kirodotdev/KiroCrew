/**
 * Overview lower-region panel slot.
 *
 * Overview ends at the deep-surface summary grid (Usage + Memory), leaving the
 * rest of the page empty. A downstream edition that needs real estate there —
 * something too big for a 150px stat tile, e.g. a scannable code or a compact
 * live chart — has nowhere to put it, and the only alternative is editing
 * OverviewPage.tsx on every upstream sync.
 *
 * This slot is deliberately SINGULAR, which is the whole design:
 *
 * - One surface, one owner, one default. A second registration does not append
 *   and does not re-order; it is a collision, and `reportSeamCollision` fails
 *   loud in dev/test. So there is never layout negotiation between parties who
 *   cannot see each other, and the region always has exactly one owner.
 * - It is NOT a second card grid. `overviewStatCards` is the card-composition
 *   shape and covers the tile strip; adding another one here would multiply that
 *   shape rather than give this region an owner. The registrant renders whatever
 *   internal layout it wants inside the slot, and owns all of it.
 *
 * Registration is expected at module-load time (edition composition), before the
 * page renders — this registry is not reactive. The core registers nothing, so
 * the region stays empty in the stock build.
 */
import type { ComponentType } from 'react'
import { reportSeamCollision } from '../apps/seamCollision'

export interface OverviewPanel {
  /** Stable key, used for the ErrorBoundary scope and the collision message. */
  id: string
  /**
   * The panel. Rendered full-width below the summary grid, with no props: a
   * slot owner reads whatever it needs itself, exactly as the built-in
   * summary cards do.
   */
  component: ComponentType
}

let OVERVIEW_PANEL: OverviewPanel | null = null

/**
 * Claim the overview panel slot. First registration wins; a second one is a
 * collision (throws in dev/test, warns and is ignored in production) rather
 * than silently replacing an owner or stacking beneath it.
 */
export function registerOverviewPanel(panel: OverviewPanel): void {
  if (OVERVIEW_PANEL) {
    reportSeamCollision(
      'overviewPanel',
      `panel ${OVERVIEW_PANEL.id} already owns the overview panel slot; ignoring ${panel.id}`,
    )
    return
  }
  OVERVIEW_PANEL = panel
}

/** The registered panel, or `null` in the stock build. */
export function getOverviewPanel(): OverviewPanel | null {
  return OVERVIEW_PANEL
}
