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

/**
 * Marks a control by whether pressing it grants an agent permission: `yes` on one that does (Allow once, Approve,
 * every Trust and override choice), `no` on one that sits inside a trust-root region and grants nothing (Reject).
 * A surface where a press can be a slip reads it and does not pass a plain press on a `yes` control: the XR page's
 * live panels, where a stray pinch must never approve (`browser_harness/panel_page.py` on the gateway reads this
 * attribute by name). The nearest mark wins; with none, a trust-root region counts as `yes`.
 */
export const AGENT_GRANT_ATTR = 'data-agent-grant'
/** Spread onto a control that grants an agent permission. */
export const agentGrant = { [AGENT_GRANT_ATTR]: 'yes' } as const
/** Spread onto a control inside a trust-root region that grants nothing. */
export const agentGrantNone = { [AGENT_GRANT_ATTR]: 'no' } as const

const AgentGrantContext = createContext<'yes' | 'no' | null>(null)

/**
 * A surface whose controls grant an agent something unless one says otherwise: a card that an agent raised (a
 * proposed change, a question that lets it go on), or a Settings tab that holds such controls. The whole surface
 * is marked `yes` at its root, and only the controls in it that grant nothing are marked `no`, so a control that is
 * added later is guarded until someone marks it. What the surface renders through a portal (a menu, a popover, a
 * dialog) carries the mark too, as a trust-root region's portals do.
 */
export function AgentGrantRegion({ children }: { children?: ReactNode }) {
  return createElement(AgentGrantContext.Provider, { value: 'yes' }, createElement('div', { className: 'contents', ...agentGrant }, children))
}

/**
 * The same for a surface that draws its own root element: wrap its content, and spread `agentGrant` on the root.
 * `on={false}` is for a root that is a grant surface only some of the time (a Settings pane, by its tab): the
 * content then renders outside any surface, whatever stands above it.
 */
export function AgentGrantProvider({ children, on = true }: { children?: ReactNode; on?: boolean }) {
  return createElement(AgentGrantContext.Provider, { value: on ? 'yes' : null }, children)
}

/** The grant mark a portalled primitive spreads onto its content: present inside a grant surface only. */
export function useAgentGrantAttrs(): Partial<Record<typeof AGENT_GRANT_ATTR, 'yes' | 'no'>> {
  const grant = useContext(AgentGrantContext)
  return grant ? { [AGENT_GRANT_ATTR]: grant } : {}
}

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

/**
 * The markers a portalled primitive spreads onto its content: the trust-root marker inside a trust-root region, and
 * the grant mark inside a grant surface (`AgentGrantRegion`). One hook for both, so every primitive that already
 * re-emits the first re-emits the second.
 */
export function useGuideTrustRootAttrs(): Partial<typeof guideTrustRoot> & Partial<Record<typeof AGENT_GRANT_ATTR, 'yes' | 'no'>> {
  const grant = useAgentGrantAttrs()
  return useGuideTrustRoot() ? { ...guideTrustRoot, ...grant } : grant
}

/**
 * Marks a region the guide's panel never covers (the chat composer, and the
 * notice rows its host draws beside it): the person types there while a step
 * is shown, and the end of the reply sits right above it. Read by
 * `pageChromeBoxes`, and by the theme decor slot,
 * which cuts it out of every theme overlay (lib/themeDecorKeepClear.ts).
 */
export const GUIDE_KEEP_CLEAR_ATTR = 'data-guide-keep-clear'
export const GUIDE_KEEP_CLEAR = { [GUIDE_KEEP_CLEAR_ATTR]: '' } as const
