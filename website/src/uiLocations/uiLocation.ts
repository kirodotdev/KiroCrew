/**
 * The render-site half of a registered UI location (see `./descriptors.ts`).
 *
 * Spread it onto the element a person actually sees and clicks:
 *
 *     <div role="button" {...uiLocation('chat.older-sessions')}>…</div>
 *
 * It adds a `data-ui-location` attribute and a callback `ref` that registers
 * the element in the trusted target registry (`./targetRegistry.ts`) while
 * React has it mounted. The guide resolves a location only through that
 * registry; the attribute is for tests, e2e selectors and debugging. The find_ui
 * generator reads the spread statically to prove where the control is drawn
 * and which catalog key its label comes from.
 *
 * The element must take the ref: an intrinsic element, or a component that
 * forwards its ref to the element carrying the attribute (the generator's
 * `REF_HOSTS`, each pinned by a rendered test). An element that also has a
 * ref of its own passes it as *own*, so neither replaces the other.
 */
import type { Ref, RefCallback } from 'react'
import type { UiLocationId } from './descriptors'
import { mergeRefs, ownedRef } from './targetRegistry'

export const UI_LOCATION_ATTR = 'data-ui-location'

export type UiLocationProps<I extends string = UiLocationId> = { 'data-ui-location': I; ref: RefCallback<Element> }

export function uiLocation(id: UiLocationId, own?: Ref<Element>): UiLocationProps {
  return { [UI_LOCATION_ATTR]: id, ref: ownedRef('location', id, own) } as UiLocationProps
}

/**
 * The same props for a location id a component received as a prop (`NavItem`,
 * `SimpleSelect`, a segment host): the forwarding half, never a render site of
 * its own. Nothing when *id* is unset, so *own* stays the element's only ref.
 */
export function forwardUiLocation<T extends Element>(id: string | undefined, ...own: Array<Ref<T> | undefined>): { 'data-ui-location'?: string; ref?: RefCallback<T> } {
  const mine = own.filter((r): r is Ref<T> => !!r)
  if (!id) return mine.length ? { ref: mergeRefs<T>(...mine) } : {}
  // One own ref merges straight in (stable while it is); two through the cached pair.
  const ref: Ref<T> | undefined = mine.length === 0 ? undefined : mine.length === 1 ? mine[0] : mergeRefs<T>(...mine)
  return { [UI_LOCATION_ATTR]: id, ref: ownedRef<T>('location', id, ref) }
}
