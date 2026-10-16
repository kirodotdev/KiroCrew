import { useEffect, useMemo } from 'react'
import {
  PINNED_VIEWS, takePlainPanelShow,
  type usePanelTabs, type ViewKind,
} from '../../hooks/usePanelTabs'
import './pinnedViews.css'

/** Summary, pinned ahead of the permanent block by Pin Summary. A module
 *  constant, so the reconcile effect sees the same array on every render. */
const SUMMARY_FIRST: readonly ViewKind[] = ['summary']

/**
 * The side panel's pinned block: Files / Artifacts / Changes are ALWAYS present
 * — pinned to the front, non-closable, and never in the + menu — whether or not
 * they currently have content. Always the WHOLE list, whatever the host
 * withholds: a withdrawal (`hiddenViews`) is a render-time filter over the
 * bucket (see `visibleTabs` in SidePanel), never a deletion from it — the bucket
 * is shared with the chat page, which must find the view again.
 *
 * Pin Summary (`dashboard.side_panel_pin_summary`) puts Summary ahead of the
 * block and lands a PLAIN show of the panel on it: the toggle, a chat switch, a
 * reload. A show where something asked for a tab (a file chip, a sub-agent card,
 * an app render) keeps that tab, and a remount lands nothing (see
 * `takePlainPanelShow`). It needs session summaries ON, read strictly where the
 * + menu reads fail-open: a pinned chip put up on an unknown flag would come down
 * again when the read says off. A host that withholds Summary never pins it.
 */
export function usePinnedViews({ tabsCtl, slot, panelHidden, pinSummary, summaryOn, summaryWithheld }: {
  tabsCtl: Pick<ReturnType<typeof usePanelTabs>, 'syncPinned' | 'openView'>
  slot: string
  panelHidden: boolean
  pinSummary: boolean
  summaryOn: boolean
  summaryWithheld: boolean
}): { pinnedViews: readonly ViewKind[]; summaryPinned: boolean } {
  const { syncPinned, openView } = tabsCtl
  const summaryPinned = pinSummary && summaryOn && !summaryWithheld
  useEffect(() => { syncPinned(PINNED_VIEWS, summaryPinned ? SUMMARY_FIRST : []) }, [syncPinned, summaryPinned])
  const shownSlot = panelHidden || !slot ? null : slot
  // Gated on `summaryPinned` so the landing is not taken while the summary flag
  // is still loading on a fresh show.
  useEffect(() => {
    if (shownSlot && summaryPinned && takePlainPanelShow(shownSlot)) openView('summary', { automatic: true })
  }, [shownSlot, summaryPinned, openView])
  const pinnedViews = useMemo(() => (summaryPinned ? [...SUMMARY_FIRST, ...PINNED_VIEWS] : PINNED_VIEWS), [summaryPinned])
  return { pinnedViews, summaryPinned }
}
