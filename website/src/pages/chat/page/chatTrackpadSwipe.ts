import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { useMotionValue } from 'framer-motion'
import { takeOverDrawer } from '../../../hooks/useDrawerSwipe'
import { shownWithin, useTrackpadSwipe } from '../../../hooks/useTrackpadSwipe'
import { idleTrackpadDrag, moveTrackpadDrag, releaseTrackpadDrag, settleTrackpadDrag, speedCommitTrackpadDrag, type TrackpadPanelDrag } from '../../../hooks/trackpadPanelDrag'
import { openActivityPanel } from '../../../store/chatSlice'
import { beginPanelSlide, retirePanelSlide } from '../../../utils/panelSlide'
import type { AppDispatch } from '../../../store'

interface ChatPanelTrackpadSwipeOptions {
  isMobile: boolean
  embedded?: boolean
  /** The shell's `actbar` column the side panel is portaled into; null on a
   *  phone and in embed frames, where there is no column to drag. */
  activitySlot: HTMLElement | null
  activityOpen: boolean
  searchOpen: boolean
  sidePanelDock: 'right' | 'bottom'
  toggleAct: () => void
  dispatch: AppDispatch
  /** The session on screen. The panel's open state is per session, and a
   *  switch restores the next one's without remounting the page. */
  activeSlot: string | null
}

/**
 * Desktop: the side panel in the shell's `actbar` column follows a two-finger
 * trackpad swipe, as the Mates page's panel does (`useMembersPageTrackpadSwipe`)
 * and as a phone's finger drags the overlay.
 *
 * `useTrackpadSwipe` cuts the wheel stream into runs; `trackpadPanelDrag` turns
 * a run's summed `deltaX` into the panel's offset (`deskX`: 0 open, `travel`
 * out) and, on lift-off, applies the phone's release rule. While the drag and
 * its settle run, the column is a window onto the panel as wide as its visible
 * part (`deskSpacerW`): the panel's left edge follows the fingers and the
 * chat's right edge moves with it, since the column is an `auto` grid track.
 * The column starts at its own docked width, so nothing steps at the swap. The
 * settle runs on the main thread (this value is never registered with the
 * compositor), so the column reads every frame of it.
 *
 * Bound on the shell grid that holds both the chat and the column, so a run
 * keeps going when the panel arrives under the pointer. Only for a right-docked
 * panel, never while the find pane holds the dock or in an embed frame. The
 * left sessions drawer is a phone surface; on desktop the sessions list is a
 * column with no gesture. A rightward run with the panel closed, or a leftward
 * one with it open, is left to the browser.
 */
export function useChatPanelTrackpadSwipe({
  isMobile, embedded, activitySlot, activityOpen, searchOpen, sidePanelDock, toggleAct, dispatch, activeSlot,
}: ChatPanelTrackpadSwipeOptions) {
  const deskX = useMotionValue(0)
  const deskSpacerW = useMotionValue(0)
  const [deskSlide, setDeskSlide] = useState(false)
  const deskSlideRef = useRef(false)
  /** The column's wrapper in the shell; its first child is the panel. */
  const deskPanelWrapRef = useRef<HTMLDivElement | null>(null)
  /** The drag's travel: the column's docked width for a close, the panel's own
   *  width for an open. Set before the column swaps, never re-measured mid-drag. */
  const travelRef = useRef(0)
  const syncSpacer = useCallback(
    () => deskSpacerW.set(Math.max(0, travelRef.current - deskX.get())),
    [deskSpacerW, deskX],
  )
  useEffect(() => deskX.on('change', syncSpacer), [deskX, syncSpacer])
  // While the column slides it changes width every frame: hold the per-frame
  // work that width would cause until it docks (panelSlide).
  useLayoutEffect(() => (deskSlide ? beginPanelSlide() : undefined), [deskSlide])

  const enabled = !isMobile && !embedded && !!activitySlot && !searchOpen && sidePanelDock === 'right'
  const rootRef = useMemo(() => ({ current: activitySlot?.parentElement ?? null }), [activitySlot])
  // Updated during render, so a callback that outlives a session switch reads
  // the session now on screen, not the one it started in.
  const now = useRef({ activityOpen, toggleAct, activeSlot })
  now.current = { activityOpen, toggleAct, activeSlot }
  const drag = useRef<TrackpadPanelDrag>(idleTrackpadDrag())
  /** Where a settle in flight is heading (true: open), or null when none is. */
  const settleTo = useRef<boolean | null>(null)
  /** The session the last admitted run (live or reduced-motion) belongs to. */
  const runSlot = useRef<string | null>(null)
  /** Whether that run may still publish: cleared when it is retired. */
  const runLive = useRef(false)
  /** The session the drag in flight (and its settle) belongs to: the run that
   *  started it. A run that grabs a settle continues that drag, so it keeps it. */
  const dragSlot = useRef<string | null>(null)

  /** Publish where the panel came to rest, and hand it back to the column. One
   *  render for both, so the column never draws the old open state between.
   *  `slot` is the session the drag belonged to: the panel's open state is
   *  per session, so a drag never publishes into another one. */
  const land = useCallback((open: boolean, slot: string | null) => {
    flushSync(() => {
      const n = now.current
      if (n.activeSlot === slot) {
        if (open && !n.activityOpen) dispatch(openActivityPanel())
        else if (!open && n.activityOpen) n.toggleAct()
      }
      deskSlideRef.current = false
      setDeskSlide(false)
    })
  }, [dispatch])

  /** Drop a drag, a settle or a reduced-motion run in flight with its outcome
   *  unpublished: stop the settle (its completion never runs), forget the run
   *  (its `onSwipe` does nothing) and hand the panel back to the column. */
  const retire = useCallback(() => {
    runLive.current = false
    if (!deskSlideRef.current) return
    takeOverDrawer(deskX)
    drag.current.on = false
    settleTo.current = null
    // Dropped, not landed: the transcript holds nothing past it.
    retirePanelSlide()
    deskSlideRef.current = false
    setDeskSlide(false)
  }, [deskX])

  // Leaving the desktop layout mid-drag or mid-settle retires the drag: the
  // phone has no column, so `land` would never run and `deskSlide` would
  // outlive it. The panel's published state is left as it was.
  useEffect(() => { if (isMobile) retire() }, [isMobile, retire])
  // A session switch mid-drag or mid-settle retires it too, before the next
  // paint: the panel now shows the other session's state, and the drag was
  // about this one's. That session keeps the state it had before the drag.
  useLayoutEffect(() => { retire() }, [activeSlot, retire])
  // The panel opened or closed by anything but this gesture (the shortcut, the
  // header toggle, the store) is the person's latest word: a drag, settle or
  // reduced-motion run still in flight is retired, so its end cannot undo it.
  // `land`'s own change finds nothing left to retire: it ends the drag first.
  useLayoutEffect(() => { retire() }, [activityOpen, retire])

  /** End the drag and settle where it committed (or fell back). */
  const settle = (open: boolean, velocity: number) => {
    const d = drag.current
    d.on = false
    settleTo.current = open
    const slot = dragSlot.current
    settleTrackpadDrag(deskX, open ? 0 : d.travel, () => { settleTo.current = null; land(open, slot) }, velocity)
  }

  useTrackpadSwipe(rootRef, {
    enabled,
    wants: (dir, fromFrame) => {
      // Asked once per run, at its first event, on the live and the
      // reduced-motion path alike: the session the run is about is the one on
      // screen NOW. Every outcome of the run is checked against it.
      runSlot.current = now.current.activeSlot
      runLive.current = true
      // A frame in the open panel relays only what its own content does not
      // scroll, and may only start the close.
      if (fromFrame) return dir === 'right' && now.current.activityOpen
      // Mid-settle, either way grabs the panel where it is.
      if (settleTo.current !== null) return true
      return dir === 'left' ? !now.current.activityOpen : now.current.activityOpen
    },
    onDragStart: () => {
      if (settleTo.current !== null) {
        // Grabbed mid-settle: stop it where it is presenting the panel and
        // follow the fingers from there; the release is judged against the
        // state it was heading for.
        takeOverDrawer(deskX)
        drag.current = { on: true, startedOpen: settleTo.current, travel: drag.current.travel, base: deskX.get(), samples: [] }
        settleTo.current = null
        return
      }
      if (deskSlideRef.current) return
      dragSlot.current = runSlot.current
      const startedOpen = now.current.activityOpen
      // A close starts from the docked column: measure it BEFORE the swap, so
      // the window that replaces it is exactly as wide and the chat stays put.
      const docked = startedOpen ? deskPanelWrapRef.current?.getBoundingClientRect().width || 0 : 0
      travelRef.current = docked
      takeOverDrawer(deskX)
      deskX.set(startedOpen ? 0 : window.innerWidth)
      syncSpacer()
      flushSync(() => {
        deskSlideRef.current = true
        setDeskSlide(true)
      })
      // An open measures the panel once it is mounted, at its own width.
      const panel = deskPanelWrapRef.current?.firstElementChild as HTMLElement | null | undefined
      const travel = docked || panel?.offsetWidth || window.innerWidth
      travelRef.current = travel
      deskX.set(startedOpen ? 0 : travel)
      syncSpacer()
      drag.current = { on: true, startedOpen, travel, base: startedOpen ? 0 : travel, samples: [] }
    },
    onDragMove: (sumX, at) => {
      const d = drag.current
      if (!d.on) return false
      deskX.set(moveTrackpadDrag(d, sumX, at))
      // Fast enough toward a side: commit there now. The rest of this run
      // (its moves, its tail, its end) does nothing; a new run may grab it.
      const now = speedCommitTrackpadDrag(d)
      if (!now) return false
      settle(now.open, now.velocity)
      return true
    },
    onDragEnd: (how, at) => {
      if (!drag.current.on) return
      const { open, velocity } = releaseTrackpadDrag(drag.current, deskX.get(), how, at)
      settle(open, velocity)
    },
    // A frame's relayed wheels (an artifact in the panel, built with
    // `relayWheel`): only a frame inside this session's open panel, in the tab
    // on screen. Every other frame, a chat widget or another tab's, is ignored.
    acceptFrame: (frame) => {
      const column = activitySlot?.querySelector('[data-testid="side-panel-column"]')
      return !!column && now.current.activityOpen && shownWithin(frame, column)
    },
    // Reduced motion: one clear swipe switches the panel once, at the run's end.
    // The session it was admitted in, never the one on screen when it ends: a
    // switch before the run ends drops the swipe.
    // A run retired before it ended (a session switch, the panel toggled by
    // other means) publishes nothing.
    onSwipe: (dir) => {
      if (!runLive.current) return
      runLive.current = false
      land(dir === 'left', runSlot.current)
    },
  })

  return { deskSlide, deskSpacerW, deskPanelWrapRef }
}

export type ChatPanelTrackpadSwipe = ReturnType<typeof useChatPanelTrackpadSwipe>
