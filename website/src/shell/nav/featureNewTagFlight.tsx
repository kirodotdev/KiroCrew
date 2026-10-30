import { useCallback, useEffect, useLayoutEffect, useState } from 'react'
import { motion } from 'framer-motion'

import { OverlayPortal } from '../../components/overlayRoot'
import { landFeatureNewTag, takeFeatureNewTagFlight, type FeatureNewTagOrigin, type FeatureNewTagState } from '../../utils/featureNewTag'

/**
 * The avatar flying from a closing feature intro to a rail row's "New" tag.
 *
 * `NavBadge` renders this beside its pill. While the row's tag is `arriving`
 * the pill is on screen but invisible; this hook measures it, claims the flight
 * the intro left (`takeFeatureNewTagFlight`) and returns the two boxes to draw
 * the flyer between. Landing -- the flyer arriving, or a pointer press skipping
 * to the end -- stores the tag as `shown`, which is what makes the pill appear,
 * and sets `landed` so the pill can pop in once rather than fade in.
 *
 * Lives in the rail row and not in the intro because the intro unmounts the
 * moment "Not now" is pressed; the row is the one party that is still on screen
 * at the landing and knows where its own pill is in either rail width.
 */
export function useFeatureNewTagFlight(navId: string, state: FeatureNewTagState | null, pill: HTMLElement | null) {
  const [flight, setFlight] = useState<{ from: FeatureNewTagOrigin; to: DOMRect } | null>(null)
  const [landed, setLanded] = useState(false)
  // A layout effect: the pill must be measured on the same frame it renders,
  // before the intro's one-frame fallback decides nobody will fly.
  useLayoutEffect(() => {
    if (state !== 'arriving' || !pill || pill.getClientRects().length === 0) return
    const from = takeFeatureNewTagFlight(navId)
    if (from) setFlight({ from, to: pill.getBoundingClientRect() })
  }, [state, pill, navId])
  // The tag went away mid-flight (its page opened): nothing left to land on.
  const flying = flight !== null && state === 'arriving'
  const land = useCallback(() => {
    setFlight(null)
    setLanded(true)
    landFeatureNewTag(navId)
  }, [navId])
  useEffect(() => {
    if (!flying) return
    // A press anywhere skips to the landing: the user has moved on, and a
    // flyer still crossing the page would be in their way.
    window.addEventListener('pointerdown', land, { capture: true, once: true })
    return () => window.removeEventListener('pointerdown', land, { capture: true })
  }, [flying, land])
  return { flight: flying ? flight : null, landed, land }
}

/** Seconds from take-off to landing. */
const FLIGHT_S = 0.8

/**
 * The flyer: the intro's avatar `<img>`, redrawn at its measured box, arcing to
 * the pill while it shrinks to the pill's height, fading out as it arrives.
 * Portalled to the overlay root, so it is clipped by nothing on the way.
 */
export function FeatureNewTagFlyer({ navId, from, to, onLanded }: { navId: string; from: FeatureNewTagOrigin; to: DOMRect; onLanded: () => void }) {
  // The flyer lands centred on the pill, at the pill's height.
  const size = Math.max(14, Math.round(to.height))
  const x1 = to.left + to.width / 2 - size / 2
  const y1 = to.top + to.height / 2 - size / 2
  // An arc: up and over, cresting a little above the higher of the two ends.
  const crest = Math.max(8, Math.min(from.top, y1) - 56)
  return (
    <OverlayPortal>
      <motion.img
        aria-hidden="true"
        alt=""
        src={from.src}
        data-feature-new-tag-flyer={navId}
        className="fixed left-0 top-0 z-[65] rounded-full pointer-events-none shadow-md"
        initial={{ x: from.left, y: from.top, width: from.width, height: from.height, opacity: 1 }}
        animate={{
          x: [from.left, (from.left + x1) / 2, x1],
          y: [from.top, crest, y1],
          width: size,
          height: size,
          opacity: [1, 1, 0],
        }}
        transition={{
          duration: FLIGHT_S,
          x: { duration: FLIGHT_S, ease: 'easeInOut' },
          // Decelerates to the crest, then falls onto the pill.
          y: { duration: FLIGHT_S, ease: ['easeOut', 'easeIn'] },
          width: { duration: FLIGHT_S, ease: 'easeIn' },
          height: { duration: FLIGHT_S, ease: 'easeIn' },
          opacity: { duration: FLIGHT_S, times: [0, 0.85, 1] },
        }}
        onAnimationComplete={onLanded}
      />
    </OverlayPortal>
  )
}
