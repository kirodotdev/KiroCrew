/**
 * The companion's eyes, overlaid on the ghost body.
 *
 * The body art is a single silhouette with no eyes of its own, so the eyes are a
 * separate layer positioned from the shared geometry in `ghostEyes.ts` — the same
 * numbers the desktop app uses, so the two cannot drift apart.
 *
 * Drawn as two rounded DIVS rather than the desktop app's inline `<svg><ellipse>`:
 * `use-lucide-icons` in website/AUTOSDE.yaml blocks inline SVG in any .tsx
 * unconditionally. A div at 100% width and height with `border-radius: 50%` is the
 * same ellipse — the original's `rx="42" ry="48"` in a 100×100 box is within a
 * rounding error of the full box — so this is a rendering change, not a design one.
 *
 * Cursor tracking (task 2, ported from the desktop app's GhostEyes): when `track`
 * is on, a mousemove listener nudges both eyes a few px toward the pointer so the
 * ghost looks at the cursor. Off by default, so static previews (the gallery, the
 * chat loader) keep still eyes.
 */
import React, { useEffect, useRef, useState } from 'react'
import { GHOST_EYE_MAP, GHOST_EYE_INK, EYE_BEATS } from './ghostEyes'

/** px of eye travel toward the cursor — MAX in the desktop app's GhostEyes. */
const GAZE_MAX = 3.2
/** Full deflection once the cursor is this far from the eyes, in px — from the source. */
const GAZE_REACH = 140
/** How long an eye stays shut, and how squashed it gets — from the source's GhostEyes. */
const BLINK_MS = 130
const BLINK_SCALE_Y = 0.12
/** Gap between blinks: 2.5s plus up to 3.5s of jitter, so it never feels metronomic. */
const BLINK_MIN_MS = 2500
const BLINK_JITTER_MS = 3500

export const GhostEyeOverlay: React.FC<{
  pose?: string
  size: number
  /** Gaze offset in eye-span units, matching the desktop app's reaction offsets. */
  dx?: number
  dy?: number
  /** Follow the global cursor, as the live pet does. Off for static previews. */
  track?: boolean
  /** True while an ancestor mirrors the art, so the gaze sign must invert. */
  flipX?: boolean
  /**
   * The reaction currently playing, for its one-off eye squash (see EYE_BEATS):
   * error opens on the mascot's wince dash, curious squeezes narrow on the snap.
   */
  anim?: string | null
  /**
   * The reaction HOLDS the eyes where the footage puts them. Posed expressions snap
   * (curious flips in ~0.15s on film), so the eyes have to arrive WITH the body
   * rather than drifting in after it.
   */
  posed?: boolean
  /**
   * The art is mirrored on the right half of the screen. Gaze is SCREEN-relative,
   * so it is negated when flipped — the eyes must follow the real cursor, not the
   * mirrored coordinate system. Matches the desktop app's GhostEyes.
   */
}> = ({ pose = 'primary', size, dx = 0, dy = 0, track = false, flipX = false, anim = null, posed = false }) => {
  const eyes = GHOST_EYE_MAP[pose] || GHOST_EYE_MAP.primary
  // Offsets are expressed in eye-span units so an expression reads the same at any
  // rendered size.
  const span = Math.abs(eyes[1].x - eyes[0].x)
  const ox = dx * span
  const oy = dy * span

  const hostRef = useRef<HTMLDivElement>(null)
  const [gaze, setGaze] = useState({ x: 0, y: 0 })
  /** The active one-off squash, or null when the eyes are their normal shape. */
  const [beat, setBeat] = useState<{ sx: number; sy: number } | null>(null)
  /** Mid-blink. The cheapest aliveness signal there is, and it needs no art. */
  const [blink, setBlink] = useState(false)

  /**
   * Idle blinking, ported from the desktop app's GhostEyes: every 2.5–6s the eyes
   * squash to a line for 130ms. The interval is random so it never reads as a metronome.
   *
   * Frozen while a reaction POSES the eyes — a blink during the curious snap or the
   * ponder hold reads as a twitch rather than life. Tracking is gated on the same flag,
   * so the held expression stays exactly where the footage puts it.
   */
  useEffect(() => {
    if (posed) { setBlink(false); return }
    let t: ReturnType<typeof setTimeout>
    let shut: ReturnType<typeof setTimeout>
    const loop = () => {
      t = setTimeout(() => {
        setBlink(true)
        shut = setTimeout(() => setBlink(false), BLINK_MS)
        loop()
      }, BLINK_MIN_MS + Math.random() * BLINK_JITTER_MS)
    }
    loop()
    return () => { clearTimeout(t); clearTimeout(shut); setBlink(false) }
  }, [posed])

  // One-off squash beats (error's wince dash, curious's foreshortening squeeze).
  useEffect(() => {
    const b = anim ? EYE_BEATS[anim] : undefined
    if (!b) { setBeat(null); return }
    const on = setTimeout(() => setBeat({ sx: b.sx, sy: b.sy }), b.at)
    const off = setTimeout(() => setBeat(null), b.at + b.ms)
    return () => { clearTimeout(on); clearTimeout(off); setBeat(null) }
  }, [anim])

  useEffect(() => {
    if (!track) { setGaze({ x: 0, y: 0 }); return }
    let raf = 0
    const onMove = (e: MouseEvent) => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        // The overlay fills the pet box, so its own centre IS the pet's centre —
        // no need to plumb the pet position through, and it stays correct as the
        // companion is dragged.
        const host = hostRef.current
        if (!host) return
        const r = host.getBoundingClientRect()
        const cx = r.left + r.width / 2
        const cy = r.top + r.height / 2
        const dxp = e.clientX - cx
        const dyp = e.clientY - cy
        const n = Math.hypot(dxp, dyp) || 1
        const mag = Math.min(1, n / GAZE_REACH)
        setGaze({ x: (dxp / n) * mag, y: (dyp / n) * mag })
      })
    }
    window.addEventListener('mousemove', onMove)
    return () => { window.removeEventListener('mousemove', onMove); cancelAnimationFrame(raf) }
  }, [track])

  /**
   * Gaze offset in px, with the mirror compensated.
   *
   * An ancestor carries `scaleX(-1)` when the companion faces the other way, and a
   * mirrored parent turns a positive translateX into leftward motion on screen. So the
   * sign flips to keep the eyes tracking the REAL cursor.
   *
   * This compensation was briefly removed after the eyes looked inverted — but the
   * real cause then was a CSS animation overriding the mirror entirely, so nothing was
   * mirrored and compensating looked wrong. With the mirror rendering again, it is
   * required. The body lean compensates identically, so eyes and body always drift to
   * the same side, which is what the design requires.
   */
  const gx = (flipX ? -gaze.x : gaze.x) * GAZE_MAX
  const gy = gaze.y * GAZE_MAX

  return (
    <div
      ref={hostRef}
      style={{ position: 'absolute', left: 0, top: 0, width: size, height: size, pointerEvents: 'none' }}
      aria-hidden
    >
      {eyes.map((eye, i) => (
        <div
          key={i}
          style={{
            position: 'absolute',
            left: `${eye.x + ox}%`,
            top: `${eye.y + oy}%`,
            width: `${eye.w}%`,
            height: `${eye.h}%`,
            transformOrigin: 'center',
            // A one-off beat outranks a blink: the wince dash and the curious squeeze
            // are the expression, and a blink landing on top would cancel them out.
            transform: `translate(-50%, -50%) translate(${gx.toFixed(2)}px, ${gy.toFixed(2)}px) scale(${beat ? beat.sx : 1}, ${beat ? beat.sy : blink ? BLINK_SCALE_Y : 1})`,
            // A posed reaction snaps, so the eyes travel to the held position with
            // the body; the resting behaviour drifts more gently.
            transition: posed
              ? 'transform 90ms ease-out, left 170ms cubic-bezier(.2,.85,.3,1), top 170ms cubic-bezier(.2,.85,.3,1)'
              : 'transform 90ms ease-out, left 320ms ease-out, top 320ms ease-out',
            borderRadius: '50%',
            background: GHOST_EYE_INK,
          }}
        />
      ))}
    </div>
  )
}
