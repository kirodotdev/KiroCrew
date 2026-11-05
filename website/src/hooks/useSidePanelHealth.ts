import { useEffect, useRef } from 'react'
import { findSidePanelFaults, sidePanelDiag, type SidePanelFault } from '../utils/sidePanelHealth'
import { SIDE_PANEL_MOTION_MS } from '../pages/chat/sidePanelMount'

/** How long after an open the panel is read: past its dock tween, with room. */
export const SIDE_PANEL_HEALTH_DELAY_MS = SIDE_PANEL_MOTION_MS * 2 + 100
/** Reads a slide is waited out before it counts as stuck (about 4 s). */
export const SLIDE_WAITS = 8

export interface SidePanelHeal {
  /** Look `#activity-bar-slot` up again (useShellSlot `rev`). */
  refreshSlot: () => void
  /** Drop a trackpad slide left set (chatTrackpadSwipe `retireSlide`). */
  retireSlide: () => void
  /** Hide this chat's native browser view (desktop app). */
  hideNative: () => void
  /** Close the panel, so the header's open button comes back. */
  close: () => void
}

/** Which heal answers which fault. `zero-size`, `no-column` and
 *  `hidden-ancestor` have none: they close the panel, and the person's next
 *  open mounts a fresh dock wrapper (or only un-hides a subtree kept mounted
 *  for a live app or Browser tab, which a remount would have destroyed). */
const HEALS: Partial<Record<SidePanelFault, keyof SidePanelHeal>> = {
  'slot-detached': 'refreshSlot',
  'slide-stuck': 'retireSlide',
  'native-cover': 'hideNative',
}

/**
 * After the store opens the side panel, check that it is on screen; when it is
 * not, heal it once, read it again, and close it when it is still not there.
 *
 * The invariant this keeps: no visible panel means the header's open button
 * is shown (it is gated on `activityOpen` being false), and pressing it shows
 * the panel or, failing that, gives the button back again.
 *
 * Every fault is written to the console with the diag fields (`[side-panel-health]`).
 * There is no client-log route to the gateway, so the console is the record.
 *
 * `native-cover` is inferred, not seen (the native view is outside the DOM), so
 * it is acted on once and never counted on the second read: hiding a view that
 * was not there costs nothing, closing a panel that was fine would.
 */
export function useSidePanelHealth({ enabled, activeSlot, dock, slot, chatPane, deskSlide, nativeSuspect, heal }: {
  /** Desktop, not embedded, the store has the panel open, the find pane is closed. */
  enabled: boolean
  activeSlot: string | null
  dock: 'right' | 'bottom'
  slot: HTMLElement | null
  chatPane: () => Element | null
  deskSlide: boolean
  nativeSuspect: () => boolean
  heal: SidePanelHeal
}): void {
  // Read at the timer, not captured at schedule: a heal re-renders these.
  const live = useRef({ slot, chatPane, deskSlide, nativeSuspect, heal })
  live.current = { slot, chatPane, deskSlide, nativeSuspect, heal }
  useEffect(() => {
    if (!enabled) return
    let attempt = 0
    let slideWaits = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const check = () => {
      const n = live.current
      // A hidden window paints nothing to read: read again once it can.
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        timer = setTimeout(check, SIDE_PANEL_HEALTH_DELAY_MS)
        return
      }
      // A slide may be a live drag (the fingers moved right after the open):
      // read again later, and call it stuck only once it outlasts any drag.
      if (n.deskSlide && slideWaits < SLIDE_WAITS) {
        slideWaits += 1
        timer = setTimeout(check, SIDE_PANEL_HEALTH_DELAY_MS)
        return
      }
      const faults = findSidePanelFaults({
        slot: n.slot, chatPane: n.chatPane(), deskSlide: n.deskSlide, nativeSuspect: attempt === 0 && n.nativeSuspect(),
      })
      if (!faults.length) return
      // Inferred only, and common (a Browser tab behind another tab): hide the
      // view quietly, so the console line stays a record of real faults.
      if (faults.every(f => f === 'native-cover')) { n.heal.hideNative(); return }
      const healable = attempt === 0 && !faults.includes('hidden-ancestor')
        ? [...new Set(faults.map(f => HEALS[f]).filter((h): h is keyof SidePanelHeal => !!h && !!n.heal[h]))]
        : []
      // eslint-disable-next-line no-console
      console.warn('[side-panel-health]', JSON.stringify({ attempt, action: healable.length ? healable : ['close'], ...sidePanelDiag({ slot: n.slot, faults }) }))
      if (!healable.length) { n.heal.close(); return }
      attempt += 1
      // A new slide after the heal is a new drag: wait it out from zero.
      slideWaits = 0
      for (const h of healable) n.heal[h]?.()
      timer = setTimeout(check, SIDE_PANEL_HEALTH_DELAY_MS)
    }
    timer = setTimeout(check, SIDE_PANEL_HEALTH_DELAY_MS)
    return () => clearTimeout(timer)
  }, [enabled, activeSlot, dock])
}
