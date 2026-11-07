/**
 * Feature Preview row registry (Settings > Developer > Feature Previews).
 *
 * The core's preview cards are written out one by one in
 * `FeaturePreviewsSection.tsx`. A downstream edition that turns one of its own
 * features on and off from that page registers a row here from its entry module,
 * instead of editing the section on every upstream sync.
 *
 * A row is a plain switch over ONE boolean gateway config key. The edition also
 * names that key on the gateway (`DashboardContributor.feature_preview_config_keys`),
 * which is the only list `/api/feature-previews/edition` reads or writes; a row
 * whose key the gateway does not accept is not drawn. `appliesOnRestart` says the
 * gateway reads the key at start, so the card says a restart applies a change.
 *
 * Label and description are the edition's own text. Registration is expected at
 * module-load time (edition composition), before the page renders: this registry
 * is not reactive. The core registers none, so the page is unchanged in the stock
 * build.
 */
import { reportSeamCollision } from '../../apps/seamCollision'

export interface FeaturePreviewRowDef {
  /** Stable key for React reconciliation and de-dup. */
  id: string
  label: string
  description: string
  /** The boolean gateway config key the switch writes, e.g. `my_feature.enabled`. */
  configKey: string
  /** What the switch shows while the key is unset. */
  defaultValue: boolean
  /** The gateway reads the key at start, so a change applies on the next start. */
  appliesOnRestart: boolean
  /** Ascending sort key among registered rows (default 0). */
  order?: number
}

const ROWS: FeaturePreviewRowDef[] = []

/**
 * Register one or more Feature Preview rows. A duplicate `id` is ignored and
 * reported, so re-entrant registration (e.g. HMR) stays idempotent.
 */
export function registerFeaturePreviewRows(rows: FeaturePreviewRowDef[]): void {
  for (const r of rows) {
    if (ROWS.some(existing => existing.id === r.id)) {
      reportSeamCollision('featurePreviewRows', `row ${r.id} already registered; ignoring duplicate`)
      continue
    }
    ROWS.push(r)
  }
}

/** All registered rows, sorted by `order` (ascending, stable). */
export function getFeaturePreviewRows(): readonly FeaturePreviewRowDef[] {
  return [...ROWS].sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
}

/** Test-only: forget every registration. */
export function resetFeaturePreviewRowsForTests(): void {
  ROWS.length = 0
}
