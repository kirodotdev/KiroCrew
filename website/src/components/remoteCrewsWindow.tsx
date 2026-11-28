/**
 * The Remote Crews window: Settings → Remote Crew's panel, opened from the crew
 * switcher over whichever crew is on screen (#41685). Managing remote crews is
 * the whole window's job, not one crew's, so it opens in place instead of sending
 * the person to the current crew's Settings.
 *
 * The panel is today's, unchanged; this file only adds the window around it and
 * the open request the switcher (or an embedded pane, through the host) makes.
 * One window per dashboard: the open state is a module store, like the crew pins.
 */
import { lazy, Suspense, useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useMatch, useNavigate } from 'react-router-dom'
import { useGuardedLeave } from './NavigationLeaveGuard'
import { parsePathSegments } from './subNavParams'
import { REMOTE_CREW_SETTINGS_PATH } from '../lib/remoteCrewAddLink'
import Modal from './Modal'
import { GlobalOverlayScope } from './overlayRoot'
import { ContentSkeleton } from './ui'
import { useAppDispatch, useAppSelector } from '../store'
import { setActiveId } from '../store/instancesSlice'
import { subscribeChatHandoff } from '../utils/errorReport'
import { i18nT } from '../i18n/t'
import {
  closeRemoteCrewsWindow, getRemoteCrewsWindowState, requestRemoteCrewPanelAdd, showRemoteCrewsList, subscribeRemoteCrewsWindow, type WindowState,
} from './remoteCrewsWindowStore'

export { openRemoteCrewsWindow, subscribeRemoteCrewPanelAdd } from './remoteCrewsWindowStore'

// Lazy: the panel carries the cloud setup form, which the app core never needs.
const RemoteCrewPanel = lazy(() =>
  import('../pages/settings/RemoteCrewPanel').then(m => ({ default: m.RemoteCrewPanel })),
)

/** Mounted once by the top-level shell; renders nothing while closed. */
export default function RemoteCrewsWindow() {
  // The shell can belong to a hidden local pane. Both this window and Add's
  // nested portal must stay above whichever crew is visible, including add-only.
  return <GlobalOverlayScope><RemoteCrewsWindowContent /></GlobalOverlayScope>
}

function RemoteCrewsWindowContent() {
  const state = useSyncExternalStore(subscribeRemoteCrewsWindow, getRemoteCrewsWindowState, () => null)
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const leave = useGuardedLeave()
  const { pathname, search } = useLocation()
  const activeId = useAppSelector(s => s.instances.activeId)
  const relayActive = useAppSelector(s => s.instances.relayActive ?? null)
  const pickSeq = useAppSelector(s => s.instances.pickSeq ?? 0)
  // Settings owns its panel even while Local is hidden behind another crew.
  // A second panel would hold a competing copy of the same form drafts.
  const settingsOwnsPanel = useMatch('/settings/*') !== null
  const settingsTab = parsePathSegments('/settings', pathname)[0] || new URLSearchParams(search).get('tab')
  const atRemoteCrew = `/settings/${settingsTab}` === REMOTE_CREW_SETTINGS_PATH
  const handled = useRef<WindowState>(null)
  useEffect(() => {
    const requested = state !== handled.current
    handled.current = state
    if (!settingsOwnsPanel || !state || getRemoteCrewsWindowState() !== state) return
    // Consume even a vetoed request: a later render must not ask or open again.
    closeRemoteCrewsWindow()
    // Entering Settings with an already-open window only closes that window.
    if (!requested) return
    if (activeId !== null || relayActive) dispatch(setActiveId(null))
    if (atRemoteCrew) {
      if (state.add) requestRemoteCrewPanelAdd()
    } else {
      leave(() => {
        navigate(REMOTE_CREW_SETTINGS_PATH)
        if (state.add) requestRemoteCrewPanelAdd()
      }, REMOTE_CREW_SETTINGS_PATH)
    }
  }, [settingsOwnsPanel, state, atRemoteCrew, leave, navigate, activeId, relayActive, dispatch])
  useEffect(() => subscribeChatHandoff(() => {
    // Notification follows successful staging/leave approval, even on /chat.
    // A closed window must not steal another surface's hand-off or crew choice.
    if (!getRemoteCrewsWindowState() || settingsOwnsPanel) return
    closeRemoteCrewsWindow()
    dispatch(setActiveId(null)) // The Local action also clears the active relay.
  }), [dispatch, settingsOwnsPanel])
  const [addOpen, setAddOpen] = useState(false)
  // Move one portal host into the window without remounting the panel: a launch
  // request may still be pending when Add closes, and owns its result there.
  const [panelHost] = useState(() => document.createElement('div'))
  const attachPanel = useCallback((node: HTMLDivElement | null) => {
    if (node) node.appendChild(panelHost)
  }, [panelHost])
  // Open completes the choice even when that crew is already on screen.
  const shown = useRef({ activeId, relayActive, pickSeq })
  useEffect(() => {
    if (!state) { shown.current = { activeId, relayActive, pickSeq }; return }
    if (shown.current.activeId !== activeId || shown.current.relayActive !== relayActive
      || shown.current.pickSeq !== pickSeq) closeRemoteCrewsWindow()
  }, [state, activeId, relayActive, pickSeq])
  // Suppress in this render, before Settings can mount its panel; the effect
  // also retires the request so leaving Settings cannot reopen the window.
  if (!state || settingsOwnsPanel) return null
  // Opened for Add: only Add a Crew shows. Keep the panel for a crew, a pending
  // launch result, or a gate that prevented Add; an ordinary cancel closes it.
  const panel = (
    <Suspense fallback={state.add ? null : <ContentSkeleton rows={6} />}>
      <RemoteCrewPanel openAdd={state.add} onAddOpenChange={setAddOpen} onAddClosed={keepPanel => {
        if (!state.add) return
        if (keepPanel) showRemoteCrewsList(); else closeRemoteCrewsWindow()
      }} />
    </Suspense>
  )
  return <>
    {createPortal(panel, panelHost)}
    {state.add ? <div hidden ref={attachPanel} /> : (
      <Modal
        open
        onClose={closeRemoteCrewsWindow}
        dismissDisabled={addOpen}
        interactionDisabled={addOpen}
        title={i18nT('components.instanceTabBar.remote_crews_window')}
        maxWidth={920}
        height="86vh"
      >
        <div ref={attachPanel} />
      </Modal>
    )}
  </>
}
