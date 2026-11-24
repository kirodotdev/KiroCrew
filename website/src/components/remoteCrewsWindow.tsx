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
import { lazy, Suspense, useEffect, useRef, useSyncExternalStore } from 'react'
import Modal from './Modal'
import { ContentSkeleton } from './ui'
import { useAppSelector } from '../store'
import { i18nT } from '../i18n/t'

// Lazy: the panel carries the cloud setup form, which the app core never needs.
const RemoteCrewPanel = lazy(() =>
  import('../pages/settings/RemoteCrewPanel').then(m => ({ default: m.RemoteCrewPanel })),
)

/** Closed, open on the crew list, or opened for Add a Crew alone (`add`: the list stays hidden behind it). */
type WindowState = null | { add: boolean }

let current: WindowState = null
const listeners = new Set<() => void>()
function emit() { listeners.forEach(cb => cb()) }

/** Open the window; `add` opens it with Add a Crew's first step on top. */
export function openRemoteCrewsWindow(add = false) {
  current = { add }
  emit()
}

function closeRemoteCrewsWindow() {
  current = null
  emit()
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

/** Mounted once by the top-level shell; renders nothing while closed. */
export function RemoteCrewsWindow() {
  const state = useSyncExternalStore(subscribe, () => current, () => null)
  // Open on a row switches the crew on screen; the window has done its job then.
  const activeId = useAppSelector(s => s.instances.activeId)
  const relayActive = useAppSelector(s => s.instances.relayActive ?? null)
  const shown = useRef({ activeId, relayActive })
  useEffect(() => {
    if (!state) { shown.current = { activeId, relayActive }; return }
    if (shown.current.activeId !== activeId || shown.current.relayActive !== relayActive) closeRemoteCrewsWindow()
  }, [state, activeId, relayActive])
  if (!state) return null
  // Opened for Add: only Add a Crew shows. The panel still mounts, hidden, because
  // it holds the add forms. Once Add closes the list shows only if a crew came of
  // it, to connect or open it; a cancel closes everything.
  const panel = (
    <Suspense fallback={state.add ? null : <ContentSkeleton rows={6} />}>
      <RemoteCrewPanel openAdd={state.add} onAddClosed={added => {
        if (!state.add) return
        if (added) { current = { add: false }; emit() } else closeRemoteCrewsWindow()
      }} />
    </Suspense>
  )
  if (state.add) return <div hidden>{panel}</div>
  return (
    <Modal
      open
      onClose={closeRemoteCrewsWindow}
      title={i18nT('components.instanceTabBar.remote_crews_window')}
      maxWidth={920}
      height="86vh"
    >
      {panel}
    </Modal>
  )
}
