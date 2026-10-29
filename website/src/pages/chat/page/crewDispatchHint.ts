import { openPanelView } from '../../../hooks/usePanelTabs'
import { openActivityPanel } from '../../../store/chatSlice'

/** The Crew Mode dispatch hint's button: open the side panel on the Crew tab of
 *  `slot`. Find shares the dock and hides the panel while open, so it is closed
 *  first, as the file and folder openers do. */
export function openCrewTab(slot: string | null | undefined, dispatch: (action: ReturnType<typeof openActivityPanel>) => unknown, closeSearch: () => void): void {
  if (!slot) return
  openPanelView(slot, 'workers')
  dispatch(openActivityPanel())
  closeSearch()
}
