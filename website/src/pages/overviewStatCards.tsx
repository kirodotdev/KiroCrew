/**
 * Overview status-card registry.
 *
 * The Settings → Overview status grid is a fixed row of `StatCard`s (Uptime,
 * Sessions, …) followed by the built-in `TunnelStatus` card. A downstream
 * edition contributes its own status cards — e.g. a credential-TTL card that
 * polls an edition endpoint — by registering a component here from its entry
 * module, instead of editing OverviewPage.tsx on every upstream sync.
 *
 * Each registered entry is a self-contained component (like the core
 * `TunnelStatus`): it renders its own `StatCard` and owns its own query/state,
 * and receives a `delay` prop so it can match the grid's staggered rise-in
 * animation. OverviewPage renders them after the core cards, in `order`
 * (ascending). The core registers none, so the grid is unchanged in the stock
 * build.
 *
 * Registration is expected at module-load time (edition composition), before
 * the page renders — this registry is not reactive.
 */
import type { ComponentType } from 'react'
import { reportSeamCollision } from '../apps/seamCollision'

export interface OverviewStatCard {
  /** Stable key for React reconciliation + de-dup. */
  id: string
  /** Ascending sort key for placement among registered cards (default 0). */
  order?: number
  /** Self-contained card component. Rendered with a `delay` for stagger animation. */
  component: ComponentType<{ delay?: number }>
}

const OVERVIEW_STAT_CARDS: OverviewStatCard[] = []

/**
 * Register one or more overview status cards. A duplicate `id` is ignored and
 * logs a warning, so re-entrant registration (e.g. HMR) stays idempotent.
 */
export function registerOverviewStatCards(cards: OverviewStatCard[]): void {
  for (const c of cards) {
    if (OVERVIEW_STAT_CARDS.some(existing => existing.id === c.id)) {
      reportSeamCollision('overviewStatCards', `stat card ${c.id} already registered; ignoring duplicate`)
      continue
    }
    OVERVIEW_STAT_CARDS.push(c)
  }
}

/** All registered overview status cards, sorted by `order` (ascending, stable). */
export function getOverviewStatCards(): readonly OverviewStatCard[] {
  return [...OVERVIEW_STAT_CARDS].sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
}
