import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppStore } from '../../store'
import { setActiveId } from '../../store/instancesSlice'
import { switchSlot } from '../../store/chatSlice'
import { applyNavIntentInMain, chatDeepLinkSlot } from '../../utils/navIntent'
import { useIsCurrentUrl, useMayLeaveForNavigation } from '../../components/NavigationLeaveGuard'

/**
 * Open an in-app path in this window: the native app menu's entries, the Crew
 * Companion's "Open session", and in-app links (`utils/internalNav`) all land
 * here, so each lands the same way.
 *
 * A session deep link takes the same route a popout's nav intent does — select
 * the session, then navigate — rather than a bare navigate. `?sid=` is read by
 * ChatPage only while it MOUNTS, so from an already-open /chat a bare navigate
 * would surface the dashboard with the previous session still on screen.
 *
 * Every other path asks the page on screen first, like a sidebar click. A path
 * that is the address already on screen (pathname AND query, `useIsCurrentUrl`)
 * unmounts nothing, so it is not asked about, as the sidebar does not ask.
 */
export function useOpenInAppPath(): (path: string) => void {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const appStore = useAppStore()
  const mayLeaveForNavigation = useMayLeaveForNavigation()
  const isCurrentUrl = useIsCurrentUrl()
  return useCallback((path: string) => {
    const mayLeave = () => isCurrentUrl(path) || mayLeaveForNavigation()
    const slotKey = chatDeepLinkSlot(path)
    if (slotKey) {
      // The session lives in the local Crew. Over a Remote Crew pane, bring
      // the local Crew forward first (as the notification banner does), so
      // the leave prompt and the switched session are both on screen.
      if (appStore.getState().instances?.activeId != null) dispatch(setActiveId(null))
      applyNavIntentInMain(
        // `path` is deliberately dropped in favour of the bare route: a
        // NavIntent carries no query string, and ChatPage writes `?sid=` back
        // into the URL itself once the session is active.
        { path: '/chat', slotKey },
        {
          navigate,
          switchSlot: (key) => { dispatch(switchSlot({ key, announceOnMissing: true })) },
          mayLeave,
        },
      )
      return
    }
    if (!mayLeave()) return
    navigate(path)
  }, [navigate, dispatch, mayLeaveForNavigation, isCurrentUrl, appStore])
}
