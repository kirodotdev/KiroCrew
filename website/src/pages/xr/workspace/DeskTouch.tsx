/**
 * A finger's touch on the desk, frame by frame (`deskTouch.ts`): each hand's index tip, read from the joints that the
 * set-up reads too (`deskSetup.ts`, `deskHands`), against the pads and tiles that are drawn (`touchTargets`). A touch
 * takes the one press as a pinch does (`pinchRoute.touch`): the first down of either owns it until its up. While the
 * tip of a pointing hand is within 5 cm of a pad's or tile's box, from any side, the dot is off and no pinch presses
 * (`pinchRoute.tipNear`). Draws nothing. It says each down (`onDown`), as a select or a pointer does, so that what
 * waits on the person's next press hears a finger too.
 */
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo } from 'react'
import { deskHands } from './deskSetup'
import { TouchClock, nearestOf, pointing, targetUnder, touchTargets, type TouchChange, type TouchHand } from './deskTouch'
import { pinchRoute, type Handed } from './pinchRoute'

/** What a test reads of the touch: the last change of each hand, each tip's height over the desk, and whether one is near. */
export interface TouchProbe { last: { hand: Handed; change: TouchChange; at: number } | null; heightM: Record<Handed, number | null>; pointing: Record<Handed, boolean | null>; tip: Record<Handed, readonly [number, number, number] | null>; near: boolean; presses: number }

export function DeskTouch({ report, onDown }: { report?: (read: (() => TouchProbe) | null) => void; onDown?: () => void }) {
  const clocks = useMemo(() => ({ left: new TouchClock(), right: new TouchClock() }), [])
  const probe = useMemo<TouchProbe>(() => ({ last: null, heightM: { left: null, right: null }, pointing: { left: null, right: null }, tip: { left: null, right: null }, near: false, presses: 0 }), [])
  useEffect(() => {
    report?.(() => ({ ...probe, heightM: { ...probe.heightM }, pointing: { ...probe.pointing }, tip: { ...probe.tip } }))
    return () => report?.(null)
  }, [report, probe])
  useEffect(() => () => { pinchRoute.tipNear = false; pinchRoute.touch = null }, [])
  useFrame(() => {
    const deskY = pinchRoute.deskY
    const now = performance.now()
    let near = false
    for (const hand of ['left', 'right'] as const) {
      const h = deskHands[hand]
      // The set-up's points: the wrist, the palm, then the thumb, index, middle, ring and little finger tips.
      const t: TouchHand | null = h && deskY !== null ? { palm: h.points[1], index: h.points[3], middle: h.points[4], ring: h.points[5] } : null
      const height = t && deskY !== null ? t.index[1] - deskY : Infinity
      probe.heightM[hand] = Number.isFinite(height) ? height : null
      probe.pointing[hand] = t ? pointing(t) : null
      probe.tip[hand] = t ? t.index : null
      const on = t ? targetUnder(t.index, touchTargets) : null
      const may = !pinchRoute.owner && (pinchRoute.touch === null || pinchRoute.touch === hand)
      const change = clocks[hand].read(t, height, on, t ? nearestOf(t.index, touchTargets) : Infinity, now, may)
      near ||= clocks[hand].near
      if (!change) continue
      probe.last = { hand, change, at: now }
      if ('down' in change) { pinchRoute.touch = hand; onDown?.() }
      else {
        if (pinchRoute.touch === hand) pinchRoute.touch = null
        if ('up' in change && change.press) { probe.presses += 1; touchTargets.get(change.up)?.press() }
      }
    }
    pinchRoute.tipNear = near
    probe.near = near
  })
  return null
}
