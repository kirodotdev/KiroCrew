/**
 * Readout-capsule segment registry.
 *
 * The header's "readout capsule" (the bordered pill holding the connection dot,
 * system-metrics, and usage segments, divided by `|` separators and tinted red
 * when offline) is an extension slot for status readouts that must live INSIDE
 * that grouping — e.g. a credential-TTL segment or a spend segment that should
 * share the capsule's border, dividers, and offline tint rather than float as a
 * separate sibling pill (which is what `topBarWidgets` provides).
 *
 * A downstream edition registers segments here from its entry module; App.tsx
 * splices them into the capsule's `segments[]` array in `order` (ascending),
 * after the core segments. The core registers none, so the capsule is unchanged
 * in the stock build.
 *
 * Each segment component is rendered with an `offline` prop so it can match the
 * capsule's connection-state styling. Registration is expected at module-load
 * time (edition composition), before App mounts — this registry is not reactive.
 *
 * Choosing between the two slots:
 *  - inside the capsule (border/divider/offline-tint grouping) → this seam.
 *  - a standalone pill next to the capsule → `registerTopBarWidgets`.
 */
import type { ComponentType } from 'react'
import { reportSeamCollision } from './seamCollision'

export interface CapsuleSegment {
  /** Stable key for React reconciliation + de-dup. */
  id: string
  /** Ascending sort key for placement among registered segments (default 0). */
  order?: number
  /** Hide on narrow (mobile) viewports. Default false. */
  hideOnMobile?: boolean
  /** The segment. Rendered with the capsule's `offline` state so it can match tint. */
  component: ComponentType<{ offline: boolean }>
}

const CAPSULE_SEGMENTS: CapsuleSegment[] = []

/**
 * Register one or more readout-capsule segments. A duplicate `id` is ignored and
 * logs a warning, so re-entrant registration (e.g. HMR) stays idempotent.
 */
export function registerCapsuleSegment(segments: CapsuleSegment[]): void {
  for (const s of segments) {
    if (CAPSULE_SEGMENTS.some(existing => existing.id === s.id)) {
      reportSeamCollision('capsuleSegments', `segment ${s.id} already registered; ignoring duplicate`)
      continue
    }
    CAPSULE_SEGMENTS.push(s)
  }
}

/** All registered capsule segments, sorted by `order` (ascending, stable). */
export function getCapsuleSegments(): readonly CapsuleSegment[] {
  return [...CAPSULE_SEGMENTS].sort((a, b) => (a.order ?? 0) - (b.order ?? 0))
}
