import { useCallback, useState } from 'react'
import { useIsMobile } from './useIsMobile'

/**
 * Which pane of a list+detail shell is on screen.
 *
 * A desktop shows both panes side by side. A phone cannot: these shells give
 * the list column a fixed 240-288px, so at 390px the detail pane is left
 * 40-60px of content width — one or two CJK characters per line, and action
 * buttons in the detail header overlap each other. A narrow viewport therefore
 * shows exactly ONE pane and drills down: the list, then the detail with an
 * explicit way back.
 *
 * `detailOpen` is explicit state, NOT "is a row selected". Several of these
 * shells auto-select their first row so the desktop detail is never blank
 * (SkillsTab, SteeringTab), which means a selection-derived rule would open the
 * detail before the user picked anything: the list would be unreachable, and
 * Back a no-op because the auto-select effect re-fires on the next render.
 */
export function useListDetailView() {
  const isMobile = useIsMobile()
  const [detailOpen, setDetailOpen] = useState(false)

  const openDetail = useCallback(() => setDetailOpen(true), [])
  const closeDetail = useCallback(() => setDetailOpen(false), [])

  return {
    isMobile,
    /** Render the list pane. Always true on a desktop. */
    showList: !isMobile || !detailOpen,
    /** Render the detail pane. Always true on a desktop. */
    showDetail: !isMobile || detailOpen,
    /** Call from the row-select handler: drills into the detail while narrow. */
    openDetail,
    /** Call from the Back control: returns to the list while narrow. */
    closeDetail,
  }
}

/** The shape {@link useListDetailView} returns. Named so a shell that hosts the
 * state in its own context (Issue Radar, whose row handlers live in child list
 * components) can declare the field without restating the members. */
export type ListDetailView = ReturnType<typeof useListDetailView>
