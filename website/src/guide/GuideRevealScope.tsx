/**
 * `<GuideRevealScope id open>`: the owner of a container a guide may have to
 * walk the person into (the sessions sidebar, the phone's sessions drawer, a
 * shared dropdown menu, a tab panel) says whether it is open.
 *
 * It sits OUTSIDE the conditional it controls, so it can report "closed" while
 * the container's contents are unmounted. It renders no element of its own;
 * the report goes to the live registry (`./liveRegistry.ts`), where a `ui.show`
 * step naming this scope (`scope` in `guidePlans.gen.ts`, compiled by the
 * generator from `shown_by`, the registered parents and `UI_REVEAL_SCOPES`)
 * completes the moment it reads open, and a live observation reports it. The
 * id is one of this build's compiled scopes (`GuideRevealScopeId`), so an
 * owner cannot report a scope no plan knows. Children read the scope chain
 * through React context, which crosses portals. It never opens anything.
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import type { GuideRevealScopeId } from '../uiLocations/guidePlans.gen'
import { GUIDE_REVEAL_SCOPES } from '../uiLocations/guidePlans.gen'
import { GuideScopeContext, useGuideRevealScopes } from './guideRevealScopeContext'
import { dropScope, reportScope } from './liveRegistry'

export function GuideRevealScope({ id, open, children }: { id: GuideRevealScopeId; open: boolean; children?: ReactNode }) {
  const parent = useGuideRevealScopes()
  // One identity per mounted owner, so two owners of one id never erase each other.
  const [owner] = useState(() => Symbol('guide-reveal-scope'))
  useEffect(() => {
    reportScope(id, owner, open)
    return () => dropScope(id, owner)
  }, [id, open, owner])
  const value = useMemo(() => [...parent, { id, open }], [parent, id, open])
  return <GuideScopeContext.Provider value={value}>{children}</GuideScopeContext.Provider>
}

/** Wrap *children* in a scope only when *id* is given (an optional prop of a shared primitive). */
export function MaybeGuideRevealScope({ id, open, children }: { id?: GuideRevealScopeId; open: boolean; children?: ReactNode }) {
  if (!id) return <>{children}</>
  return <GuideRevealScope id={id} open={open}>{children}</GuideRevealScope>
}

/**
 * The hook form, for an owner that IS the container (a panel, the composer)
 * and has no subtree to hand a scope frame to: it only reports. `id`
 * undefined reports nothing (an owner that holds the scope in one layout only).
 */
export function useGuideRevealScope(id: GuideRevealScopeId | undefined, open: boolean): void {
  const [owner] = useState(() => Symbol('guide-reveal-scope'))
  useEffect(() => {
    if (!id) return
    reportScope(id, owner, open)
    return () => dropScope(id, owner)
  }, [id, open, owner])
}

/**
 * The compiled scope of a registered disclosure (`open:<location id>`), or
 * undefined when this build compiled none for it.
 */
export function disclosureScopeId(locationId: string): GuideRevealScopeId | undefined {
  const id = `open:${locationId}`
  return Object.hasOwn(GUIDE_REVEAL_SCOPES, id) ? (id as GuideRevealScopeId) : undefined
}

/**
 * The shared owner for a disclosure's pane (a section header that folds a
 * list away, like Older Sessions): reports `open:<location id>` from the
 * disclosure's own open state. Renders nothing.
 */
export function useGuideDisclosureScope(locationId: string, open: boolean): void {
  useGuideRevealScope(disclosureScopeId(locationId), open)
}
