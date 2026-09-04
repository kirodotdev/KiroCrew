/**
 * Host overlay slot resolution.
 *
 * Turns the `ui.overlays` declarations of the installed apps into "who owns each
 * host slot right now". The host reads the answer as data — it never names a
 * specific app — so a second overlay-contributing app costs no host change.
 *
 * Kept as a pure function over a locally pinned subset of `GET /api/apps` (same
 * reason `appNav.ts` pins its own `AppNavRecord`): a field this derivation
 * depends on cannot quietly change shape underneath it.
 */
import { hasBuiltinOverlay, isHostOverlaySlot, type HostOverlaySlot } from './overlayRegistry'

/** One `ui.overlays[]` entry as it arrives from the manifest. */
export interface AppOverlayDecl {
  id?: string
  replaces?: string
}

/** The subset of `GET /api/apps` this module reads. */
export interface OverlayAppRecord {
  name: string
  enabled?: boolean
  /** Provenance. Only `'builtin'` may claim a host overlay slot. */
  origin?: string
  manifest?: {
    ui?: {
      overlays?: AppOverlayDecl[]
    }
  }
}

/** The app + overlay that currently owns a slot. */
export interface SlotOwner {
  /** App name (the `/api/apps` key). */
  app: string
  /** Overlay id — the key into the overlay component registry. */
  overlayId: string
}

export type SlotOwners = Partial<Record<HostOverlaySlot, SlotOwner>>

/**
 * Resolve slot ownership across the installed apps.
 *
 * Only ENABLED apps can claim a slot: an app's enable state is the user's opt-in,
 * so a disabled app leaves the host's own surface in place. Apps are considered
 * in name order rather than response order, so the winner of a contested slot is
 * stable across gateway restarts instead of depending on a directory scan.
 *
 * Three declarations are reported and skipped rather than applied, because each
 * would otherwise fail as a silent absence: an unknown slot name, an overlay id
 * with no registered component, and a second claimant for a slot already owned.
 *
 * Each of those warns rather than going through `reportSeamCollision`, which throws
 * in dev/test. Throwing is right for a registration a developer wrote and can fix
 * before release; it is wrong here, because these declarations arrive from
 * installed app manifests, and a third party -- or a hand-edited app.json -- must
 * never be able to take the dashboard down. The host keeps its own surface instead.
 */
export function resolveSlotOverlays(apps: readonly OverlayAppRecord[]): SlotOwners {
  const owners: SlotOwners = {}
  // Byte order, not a locale comparison: `name` is the `/api/apps` identifier, and
  // which app wins a contested slot must be the same answer for every viewer.
  const sorted = [...apps].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  for (const app of sorted) {
    if (!app.enabled) continue
    // Provenance, not just enable state. An overlay id only resolves to a component
    // compiled into this bundle, so a non-builtin app cannot supply its own -- but it
    // CAN name a builtin's id, and that is the reachable case: a self-managed app
    // persists its own manifest through the register path, so declaring
    // `command-bar` would hand it the quick-search slot and replace Cmd+K while the
    // Command Bar app itself is disabled, defeating the opt-in this surface exists
    // to provide. `builtin` is assigned only by register_builtin_apps() and is
    // refused on the self-registration path, so it is the one claim that cannot be
    // forged.
    if (app.origin !== 'builtin') {
      for (const decl of app.manifest?.ui?.overlays ?? []) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[overlaySlots] app ${app.name} (origin ${app.origin ?? 'unknown'}) may not claim host slot ${decl.replaces}; overlays are builtin-only`)
      }
      continue
    }
    for (const decl of app.manifest?.ui?.overlays ?? []) {
      const overlayId = decl.id
      const slot = decl.replaces
      if (!overlayId || !slot) continue
      if (!isHostOverlaySlot(slot)) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[overlaySlots] app ${app.name} overlay ${overlayId} claims unknown host slot ${slot}; ignoring`)
        continue
      }
      if (!hasBuiltinOverlay(overlayId)) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[overlaySlots] app ${app.name} declares overlay ${overlayId} with no registered component; ignoring`)
        continue
      }
      const held = owners[slot]
      if (held) {
        // eslint-disable-next-line no-console -- a refused contribution is invisible otherwise
        console.warn(`[overlaySlots] app ${app.name} overlay ${overlayId} wants slot ${slot}, already held by ${held.app}/${held.overlayId}; ignoring`)
        continue
      }
      owners[slot] = { app: app.name, overlayId }
    }
  }
  return owners
}
