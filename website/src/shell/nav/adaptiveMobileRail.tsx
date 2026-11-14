import { type ReactNode } from 'react'
import { useScrollEdgesY } from '../../hooks/useScrollEdges'
import { MORE_BELOW_MASK } from '../../components/ModelEffortDropdown'

/**
 * The phone rail's body: one scroll region above a pinned foot, the same rule
 * the desktop rail follows.
 *
 * - `top` (brand mark, Main rows, Discover, Library), `apps` (the app tiles and
 *   the starred mates) and `secondary` (Terminal, Customize, Kiro Account)
 *   scroll together, in that order, with a divider before `secondary`.
 * - `bottom` (Settings, Search) is always pinned below them.
 *
 * The scrollbar is hidden, so the region's bottom edge fades while anything
 * sits below the fold. The content box is observed too, so a group unfolding
 * inside the fixed-height region still remeasures.
 */
export function AdaptiveMobileRail({ top, apps, secondary, bottom, className, ...navProps }: {
  top: ReactNode
  apps: ReactNode
  secondary: ReactNode
  bottom: ReactNode
  className: string
} & Omit<React.ComponentProps<'nav'>, 'children' | 'className'>) {
  const [attachScroller, edges, , attachContent] = useScrollEdgesY<HTMLDivElement>()
  const block = 'w-full flex flex-col items-center gap-1 shrink-0'
  return (
    <nav className={className} {...navProps}>
      <div
        ref={attachScroller}
        data-testid="mobile-nav-rail-scroller"
        className={`flex-1 min-h-0 w-full overflow-y-auto overflow-x-hidden overscroll-y-none scrollbar-none${edges.bottom ? ` ${MORE_BELOW_MASK}` : ''}`}
        style={{ scrollbarWidth: 'none' }}
      >
        <div ref={attachContent} className={block}>
          {top}
          <div data-testid="mobile-nav-rail-apps" className={block}>{apps}</div>
          {/* The muted TEXT colour, not `border`: on the dark themes `border`
              is within a few shades of the rail background and the line vanished. */}
          <div role="separator" aria-hidden="true" className="w-10 h-0.5 shrink-0 rounded-full bg-muted/40 my-1.5" />
          <div data-testid="mobile-nav-rail-secondary" className={block}>{secondary}</div>
        </div>
      </div>
      <div className={`${block} pt-1`}>{bottom}</div>
    </nav>
  )
}
