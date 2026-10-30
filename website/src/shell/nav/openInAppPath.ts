import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppStore } from '../../store'
import { prefillCrewAddForm, setActiveId } from '../../store/instancesSlice'
import { useCrewDeepLink } from '../../lib/crewDeepLink'
import { REMOTE_CREW_SETTINGS_PATH, remoteCrewAddFromPath } from '../../lib/remoteCrewAddLink'
import { switchSlot } from '../../store/chatSlice'
import { applyNavIntentInMain, chatDeepLinkSlot, isAppDetailPath } from '../../utils/navIntent'
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
  // A crew link (`kirocrew://chat?crew=`) is read HERE only, never off the URL.
  const openCrewLink = useCrewDeepLink()
  return useCallback((path: string) => {
    if (openCrewLink(path)) return
    // A fragment moves within the page and unmounts nothing, so it is not part
    // of the address compared.
    const mayLeave = () => isCurrentUrl(path.split('#')[0]) || mayLeaveForNavigation()
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
    // A Remote Crew add link (`kirocrew://remote-crew/add`) only fills in the
    // local Crew's add form; the user still presses Add. Like an app link it
    // brings the local Crew forward first. The values travel in the store and
    // the route is the bare page, so the dashboard's own URL never carries them.
    const addCrew = remoteCrewAddFromPath(path)
    if (addCrew) {
      if (appStore.getState().instances?.activeId != null) dispatch(setActiveId(null))
      if (!mayLeaveForNavigation()) return
      dispatch(prefillCrewAddForm(addCrew))
      navigate(REMOTE_CREW_SETTINGS_PATH)
      return
    }
    // An app detail link names an app installed in the local Crew. Over a
    // Remote Crew pane a bare navigate would route the hidden local pane, so
    // bring the local Crew forward first, as a session link does: the leave
    // prompt then sits over the draft it is asking about.
    if (isAppDetailPath(path) && appStore.getState().instances?.activeId != null) {
      dispatch(setActiveId(null))
    }
    if (!mayLeave()) return
    navigate(path)
  }, [navigate, dispatch, mayLeaveForNavigation, isCurrentUrl, appStore, openCrewLink])
}
