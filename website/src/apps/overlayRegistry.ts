/**
 * Builtin App Overlay Registry
 *
 * Maps an app-contributed overlay id (`app.json` → `ui.overlays[].id`) to its
 * lazy-loaded React component. The page equivalent is `builtinRegistry.ts`; the
 * difference is what the key means and where the component mounts:
 *
 * - a PAGE is keyed by route and mounts inside the routed content area,
 * - an OVERLAY is keyed by id and floats above whatever the user is looking at,
 *   opened by a gesture the host owns.
 *
 * Components are lazy-loaded, so a contributed overlay costs nothing until the
 * host actually opens it.
 */
import { lazy, type ComponentType } from 'react'

/** Props every contributed overlay receives from its host slot. */
export interface OverlayProps {
  open: boolean
  onClose: () => void
}

export type LazyOverlay = React.LazyExoticComponent<ComponentType<OverlayProps>>

/**
 * Host overlay slots an app may declare via `ui.overlays[].replaces`.
 *
 * A slot is a position the host deliberately opens for replacement — it is not
 * a licence to displace arbitrary host UI. Adding a slot is a product decision,
 * so this list stays short and explicit.
 *
 * - `quick-search`: the ⌘K surface. While an enabled app claims this slot, the
 *   host routes the ⌘K gesture to that overlay instead of its own palette.
 */
export const HOST_OVERLAY_SLOTS = ['quick-search'] as const

export type HostOverlaySlot = (typeof HOST_OVERLAY_SLOTS)[number]

export function isHostOverlaySlot(slot: string): slot is HostOverlaySlot {
  return (HOST_OVERLAY_SLOTS as readonly string[]).includes(slot)
}

/**
 * Registry mapping overlay ids (from app manifest `ui.overlays[].id`) to their
 * lazy-loaded components.
 *
 * To add a builtin overlay:
 * 1. Create the component; it takes {@link OverlayProps}.
 * 2. Add an entry here: 'my-overlay': lazy(() => import('./my-app/MyOverlay'))
 * 3. Declare `ui.overlays` in your app.json manifest.
 */
export const BUILTIN_OVERLAY_REGISTRY: Record<string, LazyOverlay> = {
  'command-bar': lazy(() => import('./command-bar/CommandBarOverlay')),
}

/**
 * Registry membership is an OWN-property test, never `in`.
 *
 * Ids arrive from installed app manifests, and the slug grammar admits inherited
 * `Object.prototype` names: an app declaring `constructor` would otherwise satisfy
 * `in`, and the lookup would hand the host a function to render as a component.
 */
const registered = (id: string): boolean =>
  Object.prototype.hasOwnProperty.call(BUILTIN_OVERLAY_REGISTRY, id)

/** True when an overlay id has a registered component. */
export function hasBuiltinOverlay(id: string): boolean {
  return registered(id)
}

/** The lazy component for an overlay id, or undefined. */
export function getBuiltinOverlay(id: string): LazyOverlay | undefined {
  return registered(id) ? BUILTIN_OVERLAY_REGISTRY[id] : undefined
}
