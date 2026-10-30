/**
 * The first Mate's entrance (#23207): the moment a person first MEETS Mate.
 *
 * Drawn once, in the tab whose greeting request the server answered `started`
 * (`useFirstGreeting`'s `onStarted`), between that answer and the greeting
 * turn's first row. Mate's face appears large at the centre of the thread
 * with a one-line speech bubble, holds for `holdMs`, then flies into the
 * header name-pill's face slot (`targetRef`, held unpainted by the host until
 * `onLanded`) and lands with a small pop and a ring; `onDone` takes the whole
 * thing down. The flight is a copy portaled to the overlay root above every
 * surface, as `CrewFaceFlight` is, so neither the header's clip chain nor a
 * phone's shell top bar can cut it off.
 *
 * `prefers-reduced-motion`: no large face and no flight. The host keeps the
 * pill's face painted from the start, and only the bubble is shown, static,
 * for `holdMs` before `onDone`. The host decides this with the same reading
 * (`reduceMotion`), so the two sides agree on whether the face is in flight.
 *
 * Nothing here touches the transcript: the bubble is the entrance's own line,
 * and the greeting that follows is the server's turn, as today.
 */
import { useEffect, useRef, useState, type RefObject } from 'react'
import { animate, motion } from 'framer-motion'
import CrewStateAvatar from '../../components/CrewStateAvatar'
import Glass from '../../components/Glass'
import { OverlayPortal } from '../../components/overlayRoot'
import type { CrewmateIdentity } from '../chat/CrewmateMessage'

/** The face on stage, in CSS pixels. */
export const ENTRANCE_FACE_PX = 150
/** How long the face and bubble hold centre stage before the flight. */
export const ENTRANCE_HOLD_MS = 1500
/** The flight to the header's face slot. */
export const ENTRANCE_FLIGHT_SECS = 0.9
export const ENTRANCE_FLIGHT_EASE: [number, number, number, number] = [0.5, 0, 0.3, 1]
/** The landing pop on the slot's own face: overshoots past 1, then settles. */
export const ENTRANCE_POP_SECS = 0.45
export const ENTRANCE_POP_EASE: [number, number, number, number] = [0.2, 1.6, 0.4, 1]

export interface MateEntranceTiming {
  holdMs: number
  flightSecs: number
  popSecs: number
}

const DEFAULT_TIMING: MateEntranceTiming = {
  holdMs: ENTRANCE_HOLD_MS,
  flightSecs: ENTRANCE_FLIGHT_SECS,
  popSecs: ENTRANCE_POP_SECS,
}

type Phase = 'stage' | 'flight' | 'landed'

/** Where the stage's centre is, in viewport coordinates: the thread column's,
 *  a little above its middle, where the eye rests on an empty pane. */
function stageCentre(stage: HTMLElement | null): { x: number; y: number } {
  const r = stage?.getBoundingClientRect()
  if (!r) return { x: window.innerWidth / 2, y: window.innerHeight * 0.44 }
  return { x: r.left + r.width / 2, y: r.top + r.height * 0.44 }
}

export default function MateEntrance({
  crewmate, slotKey, text, stageRef, targetRef, reduceMotion, onLanded, onDone, timing = DEFAULT_TIMING,
}: {
  crewmate: CrewmateIdentity
  slotKey: string
  /** The bubble's one line, already translated. */
  text: string
  /** The thread column: the stage the face is centred on. */
  stageRef: RefObject<HTMLElement | null>
  /** The header name-pill's face slot the flight lands on. */
  targetRef: RefObject<HTMLElement | null>
  reduceMotion: boolean
  /** The face has landed: the host paints the slot's own face now. */
  onLanded: () => void
  /** The entrance is over; the host unmounts it. */
  onDone: () => void
  /** Test seam: the real timings are the module constants. */
  timing?: MateEntranceTiming
}) {
  const faceRef = useRef<HTMLDivElement>(null)
  const [phase, setPhase] = useState<Phase>('stage')
  const [centre] = useState(() => stageCentre(stageRef.current))
  // The ring drawn around the slot on landing, at the slot's box.
  const [ring, setRing] = useState<DOMRect | null>(null)
  // Read at fire time, so a host re-render mid-flight never restarts it.
  const callbacks = useRef({ onLanded, onDone })
  callbacks.current = { onLanded, onDone }

  // Centre stage, then the flight; under reduced motion the bubble simply goes.
  useEffect(() => {
    const timer = setTimeout(() => {
      if (reduceMotion) callbacks.current.onDone()
      else setPhase('flight')
    }, timing.holdMs)
    return () => clearTimeout(timer)
  }, [reduceMotion, timing.holdMs])

  useEffect(() => {
    if (phase !== 'flight') return
    const el = faceRef.current
    const to = targetRef.current?.getBoundingClientRect() ?? null
    if (!el || !to || to.width === 0) {
      // No slot to fly to (the header is not laid out): a cut, not a flight.
      callbacks.current.onLanded()
      callbacks.current.onDone()
      return
    }
    const from = { left: centre.x - ENTRANCE_FACE_PX / 2, top: centre.y - ENTRANCE_FACE_PX / 2 }
    // Same construction as CrewFaceFlight: the copy is drawn at its large size
    // and scaled DOWN along the way, so the landing is a scale-down of a sharp
    // raster rather than a blurry scale-up of the slot's small one.
    const place = (p: number) => {
      const left = from.left + (to.left - from.left) * p
      const top = from.top + (to.top - from.top) * p
      const side = ENTRANCE_FACE_PX + (to.width - ENTRANCE_FACE_PX) * p
      el.style.transform = `translate(${left - from.left}px, ${top - from.top}px) scale(${side / ENTRANCE_FACE_PX})`
    }
    place(0)
    const flight = animate(0, 1, {
      duration: timing.flightSecs,
      ease: ENTRANCE_FLIGHT_EASE,
      onUpdate: place,
      onComplete: () => {
        setRing(to)
        setPhase('landed')
        callbacks.current.onLanded()
        // The pop is on the slot's OWN face, now painted: the copy is gone.
        const slot = targetRef.current
        if (!slot) { callbacks.current.onDone(); return }
        animate(0, 1, {
          duration: timing.popSecs,
          ease: ENTRANCE_POP_EASE,
          onUpdate: (p) => { slot.style.transform = `scale(${0.4 + 0.6 * p})` },
          onComplete: () => {
            slot.style.transform = ''
            callbacks.current.onDone()
          },
        })
      },
    })
    return () => flight.stop()
  }, [phase, centre, targetRef, timing.flightSecs, timing.popSecs])

  const onStage = phase === 'stage'
  return (
    <OverlayPortal>
      <div
        className="fixed inset-0 z-[60] pointer-events-none"
        data-testid="mate-entrance"
        data-phase={phase}
        data-reduced-motion={reduceMotion ? '' : undefined}
      >
        {!reduceMotion && (
          /* The face on stage, then in flight. Positioned by its top-left so the
             flight's transform (translate + scale from that corner) lands its box
             exactly on the slot's. Decorative: the slot's own face is the one
             assistive tech already names. */
          <div
            ref={faceRef}
            className="fixed left-0 top-0 origin-top-left will-change-transform"
            style={{
              left: centre.x - ENTRANCE_FACE_PX / 2,
              top: centre.y - ENTRANCE_FACE_PX / 2,
              width: ENTRANCE_FACE_PX,
              height: ENTRANCE_FACE_PX,
              visibility: phase === 'landed' ? 'hidden' : undefined,
            }}
            aria-hidden="true"
            data-testid="mate-entrance-face"
          >
            <motion.div
              className="rounded-full shadow-2xl"
              initial={{ scale: 0.4, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              transition={{ type: 'spring', stiffness: 260, damping: 18, mass: 0.9 }}
            >
              <CrewStateAvatar
                seed={crewmate.name}
                avatar={crewmate.avatar}
                slotKey={slotKey}
                size={ENTRANCE_FACE_PX}
                working="full"
              />
            </motion.div>
          </div>
        )}
        {/* The one line Mate says before it greets. A status, so a screen
            reader hears it once; the greeting that follows is the transcript's. */}
        <div
          // Never wider than the screen: a long line (ja, fr, de) wraps inside
          // the bubble instead of running off a 320px phone.
          className="fixed -translate-x-1/2 max-w-[calc(100vw-2rem)]"
          style={{
            left: centre.x,
            // Under the face; under reduced motion there is no face, so it
            // takes the face's place.
            top: reduceMotion ? centre.y - 24 : centre.y + ENTRANCE_FACE_PX / 2 + 14,
          }}
        >
          <motion.div
            initial={reduceMotion ? false : { opacity: 0, y: 6 }}
            animate={{ opacity: onStage ? 1 : 0, y: onStage ? 0 : -4 }}
            transition={reduceMotion ? { duration: 0 } : { duration: 0.3, delay: onStage ? 0.45 : 0 }}
          >
            <Glass
              variant="panel"
              radius={16}
              className="px-4 py-2 text-[17px] font-semibold text-text text-center glass-shadow"
              role="status"
              data-testid="mate-entrance-bubble"
            >
              {text}
            </Glass>
          </motion.div>
        </div>
        {ring && (
          /* The landing ring, once, around the slot's box. */
          <motion.span
            aria-hidden="true"
            className="fixed rounded-full border-2 border-accent"
            style={{ left: ring.left - 6, top: ring.top - 6, width: ring.width + 12, height: ring.height + 12 }}
            initial={{ scale: 0.7, opacity: 1 }}
            animate={{ scale: 1.8, opacity: 0 }}
            transition={{ duration: 0.7, ease: 'easeOut' }}
            data-testid="mate-entrance-ring"
          />
        )}
      </div>
    </OverlayPortal>
  )
}
