/**
 * BreathingOverlay — the guided 4-7-8 exercise, layered over whatever invoked it.
 *
 * Progress is driven from ELAPSED TIME via `breathStateAt`, not a chain of
 * timeouts, so the label, the count and the animation can never drift apart — they
 * all read the same function on the same frame.
 *
 * Deliberately only three things on screen: the breathing companion, the count, and
 * one label. An earlier version stacked a title, a technique hint, the count, the
 * label, dots and a button — six elements, which both overflowed the card and gave
 * the eye nowhere to settle. The nose/mouth cue is folded into the label that needs
 * it.
 *
 * Ported from the desktop app's renderer. Two deliberate substitutions: the panel's
 * own `useSkin` theme becomes the dashboard's CSS custom properties (so it inherits
 * all ~36 Kiro Crew themes for free), and `useT` becomes `i18nT`. The companion glyph
 * is a plain inline SVG here rather than the desktop `PetAvatar`, because appearance
 * packs belong to the window layer — the motion is what carries the exercise, and
 * that is fully preserved.
 */
import { X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import './breathing.css'
import { PetAvatar } from './PetAvatar'
import { i18nT } from '../../i18n/t'
import { breathStateAt, BREATH_CYCLES, type BreathState } from './breathing'

export interface BreathingOverlayProps {
  /** Called when the exercise completes on its own. */
  onDone: () => void
  /** Called when the user ends it early. */
  onEnd: () => void
}

const COMPANION_PX = 72

/**
 * The companion that breathes with the exercise — the user's CURRENT avatar, not a
 * hardcoded ghost.
 *
 * This used to draw the built-in `kiro_idle.svg` directly, so the exercise showed the
 * default ghost even when the user had picked a capybara. The comment justifying that
 * ("appearance packs belong to the window layer") did not hold: this overlay renders
 * inside `panel.tsx`, which IS a window layer, the same one that draws the live pet.
 *
 * `PetAvatar` is self-contained — it reads the active appearance from config, resolves
 * the pack's art, and re-resolves on pack/recolour events — so no appearance data has
 * to be threaded down here. It also brings the `isDefault` eye gate for free: a custom
 * pack draws its own eyes, so PetAvatar suppresses the overlay eyes that only belong to
 * the eyeless built-in ghost (drawing both is a two-pairs-of-eyes bug). The breathing
 * scale lives on the `.cc-breathe-glyph` wrapper OUTSIDE this, so every pack breathes.
 *
 * `anim={null}` holds the body still — the breath IS the motion here; a pack's own idle
 * fidget playing underneath would fight the scale.
 */
function CompanionGlyph({ size }: { size: number }) {
  return (
    <span style={{ position: 'relative', display: 'block', width: size, height: size }}>
      <PetAvatar size={size} state="idle" anim={null} />
    </span>
  )
}

/**
 * The slot marker inside the catalogue value. A PATTERN rather than a string,
 * because it is punctuation the translator must keep, not text anyone reads —
 * writing it as a literal would claim it is prose awaiting translation.
 */
const ROUTE_SLOT = /\{route\}/

export default function BreathingOverlay({ onDone, onEnd }: BreathingOverlayProps) {
  const [state, setState] = useState<BreathState>(() => breathStateAt(0))

  const startedRef = useRef<number | null>(null)
  const rafRef = useRef<number | null>(null)
  const doneFiredRef = useRef(false)

  useEffect(() => {
    const frame = (ts: number) => {
      if (startedRef.current === null) startedRef.current = ts
      const next = breathStateAt(ts - startedRef.current)
      setState(next)

      if (next.done && !doneFiredRef.current) {
        doneFiredRef.current = true
        onDone()
        return // stop the loop; the parent decides what happens next
      }
      rafRef.current = requestAnimationFrame(frame)
    }
    rafRef.current = requestAnimationFrame(frame)
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    }
  }, [onDone])

  // Escape ends it early — this is a suggestion, never a commitment.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onEnd()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onEnd])

  /**
   * The phase phrase, split so the route can be styled down while keeping the
   * translation's WORD ORDER. English puts the manner after the verb ("Inhale
   * through your nose"); Chinese puts it before ("用鼻子吸气"). Rendering the verb
   * and route as two fixed slots would force English order on every language.
   */
  const phrase: Array<{ text: string; muted: boolean }> = (() => {
    if (state.ready) {
      return [{ text: i18nT('apps.crewCompanion.breathe.ready'), muted: false }]
    }
    if (state.phaseIndex === 1) {
      return [{ text: i18nT('apps.crewCompanion.breathe.hold'), muted: false }]
    }
    const inhaling = state.phaseIndex === 0
    const route = inhaling
      ? i18nT('apps.crewCompanion.breathe.through_nose')
      : i18nT('apps.crewCompanion.breathe.through_mouth')
    const tmpl = inhaling
      ? i18nT('apps.crewCompanion.breathe.inhale_with_route')
      : i18nT('apps.crewCompanion.breathe.exhale_with_route')

    // Split on the slot MARKER as a pattern, not a string literal — each language
    // places the
    // manner phrase differently, so the position is part of the TRANSLATION and
    // the code only splits on the marker rather than ordering the words itself.
    return tmpl.split(ROUTE_SLOT).flatMap((part, i) => [
      ...(i > 0 ? [{ text: route, muted: true }] : []),
      ...(part ? [{ text: part, muted: false }] : []),
    ])
  })()

  /**
   * The ring breathes with the companion, but not quite as far.
   *
   * If the ring scaled by exactly the same factor, the pair would be a uniform
   * zoom — the ring would stop being a reference you can judge the companion
   * against, and the breath would read as the camera moving rather than the lungs
   * filling. A small amount of damping keeps both readings.
   */
  const RING_DAMPING = 0.9
  const ringScale = 1 + (state.phase.scale - 1) * RING_DAMPING

  /** The hold has no scale change, so it gets an opacity pulse instead — the
   *  screen is never fully static. */
  const isHold = state.phaseIndex === 1

  return (
    <div className="cc-breathe" role="dialog" aria-modal="true"
         aria-label={i18nT('apps.crewCompanion.breathe.start')}>
      <button
        type="button"
        onClick={onEnd}
        className="cc-breathe-end"
        aria-label={i18nT('apps.crewCompanion.breathe.end')}
      >
        <X size={14} aria-hidden="true" />
      </button>

      {/* The companion supplies the MOTION, the number the COUNT, one label the
          action. Stacked, never superimposed — centring the digit on the glyph made
          both unreadable. */}
      <div className="cc-breathe-stage">
        <span
          className={`cc-breathe-ring${isHold ? ' cc-breathe-pulse' : ''}`}
          style={{
            transform: `scale(${ringScale})`,
            transition: `transform ${state.phase.ms}ms cubic-bezier(.4,.0,.4,1)`,
          }}
        />
        <span
          className="cc-breathe-glyph"
          style={{
            transform: `scale(${state.phase.scale})`,
            // Matches the phase duration, so the motion IS the timing.
            transition: `transform ${state.phase.ms}ms cubic-bezier(.4,.0,.4,1)`,
          }}
        >
          <CompanionGlyph size={COMPANION_PX} />
        </span>
      </div>

      {/* Tabular figures so the digit does not jitter as its width changes. */}
      <div className="cc-breathe-count" aria-live="off">{state.secondsLeft}</div>

      <div className="cc-breathe-label">
        {phrase.map((seg, i) => (
          <span key={i} className={seg.muted ? 'cc-breathe-route' : undefined}>
            {seg.text}
          </span>
        ))}
      </div>

      {/* Breath progress as quiet dots — reassurance, not something to track. */}
      <div className="cc-breathe-dots">
        {Array.from({ length: BREATH_CYCLES }, (_, i) => (
          <span
            key={i}
            className={`cc-breathe-dot${i < state.cycle ? ' cc-breathe-dot-on' : ''}`}
          />
        ))}
      </div>
    </div>
  )
}
