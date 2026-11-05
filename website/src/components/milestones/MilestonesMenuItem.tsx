import type { ComponentType, ReactNode } from 'react'
import { MilestoneRing, indicatorLabel, useIndicatorState } from './MilestonesIndicator'
import { openMilestonesPanel } from './useMilestones'
import { i18nT } from '../../i18n/t'

type MenuItemComponent = ComponentType<{ className?: string; onSelect?: (event: Event) => void; 'aria-label'?: string; 'data-testid'?: string; children?: ReactNode }>

/** The Explore row of the phone overflow menu: the same ring, dot and streak
 *  as the top-bar indicator, which this cell has no room for while a
 *  session's menu is in it (two controls per row). Nothing until enabled,
 *  or until a read fails for a reason other than a missing route. */
export default function MilestonesMenuItem({ Item }: { Item: MenuItemComponent }) {
  const { shown, done, total, streak, dot } = useIndicatorState()
  if (!shown) return null
  return (
    <Item className="[@media(hover:none)]:min-h-10" onSelect={() => openMilestonesPanel()} aria-label={indicatorLabel(done, total, streak)} data-testid="mobile-milestones-item">
      <span className="flex items-center gap-2">
        <MilestoneRing done={done} total={total} dot={dot} />
        <span>{i18nT('milestones.title')}</span>
        {streak >= 1 && <span className="text-muted tabular-nums">{i18nT('milestones.streak_short', { count: streak })}</span>}
      </span>
    </Item>
  )
}
