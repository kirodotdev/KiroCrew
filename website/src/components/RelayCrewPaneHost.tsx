/**
 * Keeps the desktop shell's relay Crew panes in step with the tab bar: the
 * active pane covers <main> at its rect and hides under a local menu or dialog,
 * every other open pane stays loaded but hidden, past the warm cap the least
 * recent hidden one closes, and a pane main closes drops its tab, so every tab
 * has an open pane. Mounted only while the shell has the pane calls
 * (lib/relayCrewPanes.ts).
 */
import { useEffect } from 'react'
import { useAppDispatch, useAppSelector } from '../store'
import { relayPaneClosed } from '../store/instancesSlice'
import { RELAY_PANE_OVERLAY_SELECTOR, RELAY_PANE_WARM_CAP, relayPaneBridge, relayPaneRect } from '../lib/relayCrewPanes'

const NO_IDS: string[] = []

/** A menu or dialog on screen outside <main>; <main> is hidden under the pane, so its own lists do not count. */
const overlayUp = () =>
  Array.from(document.querySelectorAll(RELAY_PANE_OVERLAY_SELECTOR)).some(el => !el.closest('#main-content'))

export default function RelayCrewPaneHost() {
  const dispatch = useAppDispatch()
  const active = useAppSelector(s => s.instances.relayActive ?? null)
  const live = useAppSelector(s => s.instances.relayLive ?? NO_IDS)

  useEffect(() => relayPaneBridge()?.onPaneEvent(e => {
    if (e.type === 'closed') dispatch(relayPaneClosed(e.endpointId))
  }), [dispatch])

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

  return null
}
