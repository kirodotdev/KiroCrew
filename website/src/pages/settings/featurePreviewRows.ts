/**
 * Feature Preview row registry (Settings > Developer > Feature Previews).
 *
 * The core's preview cards are written out one by one in
 * `FeaturePreviewsSection.tsx`. A downstream edition that turns one of its own
 * features on and off from that page registers a row here from its entry module,
 * instead of editing the section on every upstream sync.
 *
 * Each registered entry is a self-contained component, like an
 * `overviewStatCards` entry: it renders its own card and switch and owns its own
 * read and write, through the edition's own route (`contribute_routes`) and API
 * module (`apiTransport`). The section renders the rows after the core cards and
 * before the Decisions card, in registration order, each inside its own
 * ErrorBoundary. The core registers none, so the page is unchanged in the stock
 * build.
 *
 * Registration is expected at module-load time (edition composition), before
 * the page renders: this registry is not reactive.
 */
import type { ComponentType } from 'react'
import { reportSeamCollision } from '../../apps/seamCollision'

export interface FeaturePreviewRow {
  /** Stable key for React reconciliation, de-dup and the ErrorBoundary scope. */
  id: string
  /** Self-contained row: its own card, switch, read and write. */
  component: ComponentType
}

const ROWS: FeaturePreviewRow[] = []

/**
 * Register one or more Feature Preview rows. A duplicate `id` is a collision
 * (`reportSeamCollision`) and is ignored.
 */
export function registerFeaturePreviewRows(rows: FeaturePreviewRow[]): void {
  for (const r of rows) {
    if (ROWS.some(existing => existing.id === r.id)) {
      reportSeamCollision('featurePreviewRows', `row ${r.id} already registered; ignoring duplicate`)
      continue
    }
    ROWS.push(r)
  }
}

/** All registered rows, in registration order. */
export function getFeaturePreviewRows(): readonly FeaturePreviewRow[] {
  return ROWS
}

/** Test-only: forget every registration. */
export function resetFeaturePreviewRowsForTests(): void {
  ROWS.length = 0
}
