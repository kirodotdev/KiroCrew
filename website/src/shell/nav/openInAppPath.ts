import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch, useAppStore } from '../../store'
import { localCovered, prefillCrewAddForm, setActiveId } from '../../store/instancesSlice'
import { useCrewDeepLink } from '../../lib/crewDeepLink'
import { REMOTE_CREW_SETTINGS_PATH, remoteCrewAddFromPath } from '../../lib/remoteCrewAddLink'
import { setAgentSwitchNotice, switchSlot } from '../../store/chatSlice'
import { openClosedSession } from '../../lib/openClosedSession'
import { i18nT } from '../../i18n/t'
import { sessionKeyFrom } from '../../utils/sessionKeys'
import { isMissingSlotError } from '../../utils/thunkError'
import { applyNavIntentInMain, chatDeepLinkSlot, isAppDetailPath, isMembersPath } from '../../utils/navIntent'
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
  // A crew link (`kirocrew://chat?crew=`) switches panes and connects only from
  // HERE; the same route on the dashboard's own address only opens the crew window.
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
      if (localCovered(appStore.getState())) dispatch(setActiveId(null))
      // Switch first: the roster can lag a session that was just opened (the
      // Crew Companion's "Open session"), so only the server's 404 says the
      // slot is not open. A full key that 404s may still be a closed session on
      // disk, so it is resumed as a closed-session chip does: probe the one key
      // and tell a failed lookup from a confirmed deletion.
      const switchOrResume = (key: string) => {
        const closedKey = sessionKeyFrom(key)
        void dispatch(switchSlot({ key, announceOnMissing: true, quietOnMissing: closedKey !== null, leaveSplit: true })).then(action => {
          if (!closedKey || !switchSlot.rejected.match(action) || !action.payload || !isMissingSlotError(action.payload)) return
          // A later switch owns the pane now; do not pull it back.
          const stillCurrent = () => appStore.getState().chat.slotSwitchLatestRequestId === action.meta.requestId
          if (!stillCurrent()) return
          const showActionError = (message: string) => { dispatch(setAgentSwitchNotice({ message, error: true })) }
          void openClosedSession({ key: closedKey, title: closedKey, leaveSplit: true }, dispatch, showActionError, stillCurrent).then(outcome => {
            if (outcome === 'failed' && stillCurrent()) {
              showActionError(i18nT('components.markdownRenderer.closed_session_open_failed', { name: closedKey }))
            }
          })
        })
      }
      applyNavIntentInMain(
        // `path` is deliberately dropped in favour of the bare route: a
        // NavIntent carries no query string, and ChatPage writes `?sid=` back
        // into the URL itself once the session is active.
        { path: '/chat', slotKey },
        {
          navigate,
          switchSlot: switchOrResume,
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
      if (localCovered(appStore.getState())) dispatch(setActiveId(null))
      if (!mayLeaveForNavigation()) return
      dispatch(prefillCrewAddForm(addCrew))
      navigate(REMOTE_CREW_SETTINGS_PATH)
      return
    }
    // An app detail or Bots page link names an app or Bot in the local Crew. Over a
    // Remote Crew pane a bare navigate would route the hidden local pane, so
    // bring the local Crew forward first, as a session link does: the leave
    // prompt then sits over the draft it is asking about.
    if ((isAppDetailPath(path) || isMembersPath(path)) && localCovered(appStore.getState())) {
      dispatch(setActiveId(null))
    }
    if (!mayLeave()) return
    navigate(path)
  }, [navigate, dispatch, mayLeaveForNavigation, isCurrentUrl, appStore, openCrewLink])
}
