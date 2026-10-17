/**
 * The React side of reveal scopes (see `./GuideRevealScope.tsx`): each scope a
 * subtree sits in, outermost first. Carried by React context, not DOM
 * ancestry, so a portal (a shared menu's content, a drawer rendered at the
 * body) is still inside the scope its owner opened.
 */
import { createContext, useContext } from 'react'

export interface GuideScopeFrame {
  id: string
  open: boolean
}

export const GuideScopeContext = createContext<readonly GuideScopeFrame[]>([])

/** The reveal scopes this component renders inside, outermost first. */
export function useGuideRevealScopes(): readonly GuideScopeFrame[] {
  return useContext(GuideScopeContext)
}
