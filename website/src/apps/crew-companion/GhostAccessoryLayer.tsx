/**
 * The ghost's dress-up prop layer, drawn over (or behind) the body.
 *
 * ## The art is the real art, from files
 *
 * Six of the seven props are SVG ASSETS traced in Figma — `assets/props/*.svg`,
 * 216 KB of vector between them. Only the sunglasses are inline, because they are
 * small enough to be. A first attempt at this port tried to inline all seven as
 * `data:` URIs and, being unable to carry 216 KB of paths, ended up substituting
 * simplified stand-ins. That is worse than not shipping the feature: the mascot is
 * the product's face, and approximated art reads as a bug nobody can name. The
 * assets were copied across instead, exactly as `kiro_idle.svg` already was.
 *
 * ## Positioning is derived from the EYES, not from the box
 *
 * Every prop is placed relative to the eye pair's midpoint and span, so a prop
 * lands correctly in each posed eye position rather than at a fixed offset that
 * only looks right in one pose. The multipliers below are carried over verbatim
 * from the desktop app, where they were tuned 1:1 against the Figma reference —
 * they are measurements, not preferences, so they are not rounded or "tidied".
 *
 * ## Two placements that look wrong until you know why
 *
 * The FLOWER sits BEHIND the body (`zIndex: -1`) and carries an unconditional
 * `scaleX(-1)`. Its natural orientation matches the reference in the facing-LEFT
 * pose, and the parent ghost art applies its own `scaleX(-1)` when facing left, so
 * this one cancels that — which makes the tilt mirror correctly in BOTH facings.
 * Removing it "to simplify" flips the bouquet in one direction only.
 *
 * The sunglasses' temple leg deliberately overflows the viewBox
 * (`overflow: 'visible'`), and eyes are hidden underneath them by the caller
 * consulting `HIDES_EYES` — this component does not know about eyes.
 */
import React from 'react'

import { GHOST_EYE_MAP } from './ghostEyes'
import type { GhostAccessory } from './ghostAccessories'

import antennaUrl from './assets/props/antenna.svg'
import coffeeUrl from './assets/props/coffee.svg'
import flowerUrl from './assets/props/flower.svg'
import partyHatUrl from './assets/props/partyhat.svg'
import popperUrl from './assets/props/popper.svg'
import sleepMaskUrl from './assets/props/sleepmask.svg'
import sunglassesUrl from './assets/props/sunglasses.svg'

export interface GhostAccessoryLayerProps {
  /** Which prop to draw. `'none'` draws nothing. */
  id: GhostAccessory
  /** Eye pose key, so the prop follows the eyes into each posed position. */
  pose: string
  /** True when the body is mirrored; the flower alone reacts to it (see above). */
  flipX?: boolean
}

/** Art shared by every asset-backed prop. */
const IMG: React.CSSProperties = { width: '100%', display: 'block' }

const GhostAccessoryLayer: React.FC<GhostAccessoryLayerProps> = ({ id, pose }) => {
  if (id === 'none') return null

  const es = GHOST_EYE_MAP[pose] || GHOST_EYE_MAP.primary
  /*
   * POSITION follows the eyes; SIZE does not.
   *
   * Every prop below is measured in `span` units, and `span` used to be the CURRENT
   * pose's eye distance. But the poses are expressions, not different heads: `primary`
   * spans 15.5% of the box while `4th` (the celebrate squint) spans 11.5%, so every
   * prop rendered a quarter smaller during a celebration than at rest — a party hat
   * that shrank when the companion was happy, and shades that changed size with the
   * expression behind them. A hat is a physical object; it does not resize because the
   * face under it squinted.
   *
   * So the anchor (mx, my) still comes from the live pose — a prop must follow the
   * face it sits on — while every LENGTH is measured against a fixed reference span.
   * `primary` is that reference because the props were authored against it.
   */
  const mx = (es[0].x + es[1].x) / 2
  const my = (es[0].y + es[1].y) / 2
  const ref = GHOST_EYE_MAP.primary
  const span = Math.abs(ref[1].x - ref[0].x)

  /** Percentage-positioned box centred on (left, top). */
  const box = (left: number, top: number, width: number): React.CSSProperties => ({
    position: 'absolute',
    left: `${left}%`,
    top: `${top}%`,
    width: `${width}%`,
    transform: 'translate(-50%, -50%)',
    pointerEvents: 'none',
  })

  if (id === 'sunglasses') {
    /*
     * An asset like the other six, not an inline vector.
     *
     * The traced Figma vector (node 2344:321) lived inline here because the source app
     * had it inline. This repo forbids hand-authored vector path markup in a .tsx — the
     * `use-lucide-icons` rule, and it is blocking — and the rule is pointing at
     * something real: with the paths in a file, all seven props load the same way and
     * the component stops being half art and half markup.
     *
     * The lenses land ON the eyes, which is why the caller suppresses the eye layer
     * for this prop (HIDES_EYES); the temple leg deliberately overflows the viewBox,
     * so the asset carries `overflow="visible"`.
     */
    return (
      <div style={box(mx, my + 2, span * 3.1)}>
        <img src={sunglassesUrl} alt="" style={IMG} />
      </div>
    )
  }

  if (id === 'flower') {
    // A bouquet tucked BEHIND the ghost (Figma node 7:670): flowers above the
    // crown, wrapper and bow at the cheek. The SVG bakes the design's flip and
    // -149.83° rotation, so its viewBox is the rotated AABB (153.9×170.2).
    // dx=-2.31, dy=+0.349, w=3.422 eye-spans — tuned 1:1 against the reference.
    return (
      <div style={{
        ...box(mx - span * 2.31, my + span * 0.349, span * 3.422),
        transform: 'translate(-50%, -50%) scaleX(-1)',
        zIndex: -1,
      }}>
        <img src={flowerUrl} alt="" style={IMG} />
      </div>
    )
  }

  if (id === 'partyhat') {
    return (
      <div style={box(mx - span * 1.64, my - span * 2.28, span * 2.832)}>
        <img src={partyHatUrl} alt="" style={IMG} />
      </div>
    )
  }

  if (id === 'antenna') {
    return (
      <div style={box(mx - span * 0.73, my - span * 1.33, span * 4.91)}>
        <img src={antennaUrl} alt="" style={IMG} />
      </div>
    )
  }

  if (id === 'sleepmask') {
    return (
      <div style={box(mx - span * 0.5, my - span * 0.051, span * 4.34)}>
        <img src={sleepMaskUrl} alt="" style={IMG} />
      </div>
    )
  }

  if (id === 'popper') {
    return (
      <div style={box(mx + span * 3.131, my + span * 0.724, span * 3.542)}>
        <img src={popperUrl} alt="" style={IMG} />
      </div>
    )
  }

  if (id === 'coffee') {
    return (
      <div style={box(mx + span * 1.175, my + span * 1.565, span * 1.203)}>
        <img src={coffeeUrl} alt="" style={IMG} />
      </div>
    )
  }

  // An unknown id draws nothing rather than a placeholder: a wrong prop on the
  // mascot's face is more confusing than an absent one.
  return null
}

export default GhostAccessoryLayer
