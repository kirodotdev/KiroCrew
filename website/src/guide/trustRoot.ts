/**
 * The markers a guide reads off the page, independent of the language shown
 * (`./findTargetPolicy.ts`). A leaf module, so a shared component can mark
 * itself without importing the guide.
 *
 * - `data-guide-trust-root`: a region of the agent's own ceiling (a trust-root
 *   settings panel, an approval prompt, a trust dialog). A guide never points
 *   at, resolves or reports a control inside it.
 * - `data-guide-caution`: a control that removes something, whatever its
 *   label says, so a guide shows its caution line and a press alone never
 *   ends the guide. For a control whose name is built at run time (`Delete
 *   {{name}}`), where no catalog key can be read back from the name.
 * - `data-guide-target`: the location id a shared primitive's row answers to.
 *
 * A trust-root region also marks what it renders through a portal: the
 * marker is an ancestor in the DOM only for what renders inside the region,
 * so the region provides `GuideTrustRootProvider` and every shared primitive
 * that portals its content (menus, popovers, dialogs, modals, tips) re-emits
 * the marker on that content (`useGuideTrustRootAttrs`). React context
 * crosses portals; DOM ancestry does not.
 */
import { createContext, createElement, useContext, type ReactNode } from 'react'

export const GUIDE_TRUST_ROOT_ATTR = 'data-guide-trust-root'
export const GUIDE_CAUTION_ATTR = 'data-guide-caution'
/**
 * The registered location a row drawn by a shared primitive answers to (a
 * Settings sub-page row: `settings.sub.<tab>.<key>`), for a guide that looks
 * a control up by its location id. The runtime twin of `data-ui-location`
 * for an id the primitive builds from its props, which the index generator
 * cannot read off the source; the index registers those ids itself.
 */
export const GUIDE_TARGET_ATTR = 'data-guide-target'

/** Spread onto a trust-root region's outermost element. */
export const guideTrustRoot = { [GUIDE_TRUST_ROOT_ATTR]: '' } as const

/** Spread onto a control that removes something (see the module comment). */
export const guideCaution = { [GUIDE_CAUTION_ATTR]: '' } as const

const GuideTrustRootContext = createContext(false)

/** Everything rendered under it, portals included, is part of a trust-root region. */
export function GuideTrustRootProvider({ children }: { children?: ReactNode }) {
  return createElement(GuideTrustRootContext.Provider, { value: true }, children)
}

/**
 * A trust-root region in one element: the marker on a layout-neutral
 * (`display: contents`) wrapper, and the provider for what it portals.
 */
export function GuideTrustRootRegion({ children }: { children?: ReactNode }) {
  return createElement(GuideTrustRootProvider, null, createElement('div', { className: 'contents', ...guideTrustRoot }, children))
}

/** Whether the caller renders inside a trust-root region. */
export function useGuideTrustRoot(): boolean {
  return useContext(GuideTrustRootContext)
}

/** The marker a portalled primitive spreads onto its content: present inside a trust-root region only. */
export function useGuideTrustRootAttrs(): Partial<typeof guideTrustRoot> {
  return useGuideTrustRoot() ? guideTrustRoot : {}
}

/**
 * Marks a region the guide's panel never covers (the chat composer): the
 * person types there while a step is shown, and the end of the reply sits
 * right above it. Read by `pageChromeBoxes`.
 */
export const GUIDE_KEEP_CLEAR_ATTR = 'data-guide-keep-clear'
export const GUIDE_KEEP_CLEAR = { [GUIDE_KEEP_CLEAR_ATTR]: '' } as const
