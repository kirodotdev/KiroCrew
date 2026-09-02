/**
 * Top-bar widget registry.
 *
 * The header's right-hand actions area (next to the readout capsule) is an
 * extension slot: a downstream edition mounts its own status widgets — e.g. a
 * credential-TTL capsule or a spend pill — by registering them here from its
 * entry module, instead of editing App.tsx on every upstream sync.
 * App.tsx renders every registered widget in insertion order. The core
 * registers none, so the slot is empty in the stock build.
 *
 * Scope: registration is expected at module-load time (edition composition),
 * before App mounts — this registry is not reactive, so registering after the
 * header has rendered will not appear until the next unrelated re-render.
 */
import type { ComponentType } from 'react'
import { reportSeamCollision } from './seamCollision'

export interface TopBarWidget {
  /** Stable key for React reconciliation + de-dup. */
  id: string
  /** The widget component. Rendered with no props; reads its own state/queries. */
  component: ComponentType
}

const TOP_BAR_WIDGETS: TopBarWidget[] = []

/**
 * Register one or more top-bar widgets. A duplicate `id` is ignored and logs a
 * warning, so re-entrant registration (e.g. HMR) stays idempotent.
 */
export function registerTopBarWidgets(widgets: TopBarWidget[]): void {
  for (const w of widgets) {
    if (TOP_BAR_WIDGETS.some(existing => existing.id === w.id)) {
      reportSeamCollision('topBarWidgets', `widget ${w.id} already registered; ignoring duplicate`)
      continue
    }
    TOP_BAR_WIDGETS.push(w)
  }
}

/** All registered top-bar widgets, in insertion order. */
export function getTopBarWidgets(): readonly TopBarWidget[] {
  return TOP_BAR_WIDGETS
}
