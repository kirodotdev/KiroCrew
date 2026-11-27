/**
 * Keeps the desktop shell's relay Crew panes in step with the tab bar: the
 * active pane covers <main> at its rect and hides under a local menu or dialog,
 * every other open pane stays loaded but hidden, past the warm cap the least
 * recent hidden one closes, and a pane main closes drops its tab, so every tab
 * has an open pane. A closed pane's Crew stays in the switcher's Remote Crews
 * list, where a click opens it again. The shell creates every pane hidden, so
 * this is the only place one is shown. Mounted while the shell has the pane
 * calls (lib/relayCrewPanes.ts). While a pane is in front, the local top bar
 * takes that Crew's dark or light mode, unsaved (hooks/themeModeOverride.ts).
 * From the click until the Crew's page has loaded (the shell keeps the pane
 * hidden until then), <main> shows the Crew's name and "Connecting…" instead of
 * a blank or black area.
 */
import { useEffect, useState } from 'react'
import RelayConnectingCover from './RelayConnectingCover'
import { useAppDispatch, useAppSelector } from '../store'
import { relayPaneClosed } from '../store/instancesSlice'
import { RELAY_PANE_OVERLAY_SELECTOR, RELAY_PANE_WARM_CAP, relayPaneBridge, relayPaneRect } from '../lib/relayCrewPanes'
import { setThemeModeOverride } from '../hooks/themeModeOverride'

const NO_IDS: string[] = []

/** A menu or dialog on screen outside <main>; <main> is hidden under the pane, so its own lists do not count. */
const overlayUp = () =>
  Array.from(document.querySelectorAll(RELAY_PANE_OVERLAY_SELECTOR)).some(el => !el.closest('#main-content'))

export default function RelayCrewPaneHost() {
  const dispatch = useAppDispatch()
  const active = useAppSelector(s => s.instances.relayActive ?? null)
  const live = useAppSelector(s => s.instances.relayLive ?? NO_IDS)
  const opening = useAppSelector(s => {
    const o = s.instances.relayOpening
    return o && o.gen === (s.instances.pickSeq ?? 0) ? o.name : null
  })
  const activeName = useAppSelector(s => s.instances.relayTabs?.find(t => t.id === s.instances.relayActive)?.name ?? null)
  // Panes whose page has loaded; the shell shows a pane only after that.
  const [loaded, setLoaded] = useState<Record<string, true>>({})

  // endpointId -> whether its page is dark, from the pane's own theme colour.
  const [dark, setDark] = useState<Record<string, boolean>>({})
  useEffect(() => relayPaneBridge()?.onPaneEvent(e => {
    if (e.type === 'closed') dispatch(relayPaneClosed(e.endpointId))
    if (e.type === 'loaded') setLoaded(prev => ({ ...prev, [e.endpointId]: true }))
    if (e.type === 'closed') setLoaded(({ [e.endpointId]: _gone, ...rest }) => rest)
    if (e.type === 'theme' || e.type === 'closed') {
      setDark(prev => {
        const { [e.endpointId]: _old, ...rest } = prev
        return e.type === 'theme' && typeof e.dark === 'boolean' ? { ...rest, [e.endpointId]: e.dark } : rest
      })
    }
  }), [dispatch])

  // Only a shell that sends `loaded` keeps a pane hidden until then; with an older one
  // the pane shows at once, and a cover waiting for the event would never go.
  const waitsForLoad = relayPaneBridge()?.paneLoadEvents === true
  // Before its page has drawn, a pane's theme colour is the boot page's, not the Crew's.
  const activeDark = active && (!waitsForLoad || loaded[active]) ? dark[active] : undefined
  useEffect(() => {
    setThemeModeOverride(activeDark === undefined ? null : activeDark ? 'dark' : 'light')
    return () => setThemeModeOverride(null)
  }, [activeDark])

  useEffect(() => {
    const bridge = relayPaneBridge()
    if (!bridge) return
    for (const id of live) if (id !== active) void bridge.setPaneInactive(id, true)
    // Its tab goes with it on the `closed` event; a second close of a closing pane does nothing.
    const victim = live.length > RELAY_PANE_WARM_CAP ? live.filter(id => id !== active).pop() : undefined
    if (victim) void bridge.closePane(victim)
    if (!active) return
    const report = () => {
      const at = relayPaneRect()
      if (at) void bridge.setPaneBounds(active, ...at)
    }
    // Where it goes before it shows.
    report()
    void bridge.setPaneInactive(active, false)
    let last: boolean | null = null
    const check = () => {
      const up = overlayUp()
      if (up === last) return
      last = up
      void bridge.setPaneOverlay(active, up)
    }
    check()
    const main = document.getElementById('main-content')
    const ro = new ResizeObserver(report)
    if (main) ro.observe(main)
    window.addEventListener('resize', report)
    const mo = new MutationObserver(check)
    mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['role', 'data-native-overlay', 'data-relay-pane-overlay'] })
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', report)
      mo.disconnect()
    }
  }, [active, live])

  const connecting = opening ?? (active && waitsForLoad && !loaded[active] ? activeName : null)
  return connecting === null ? null : <RelayConnectingCover name={connecting} />
}
