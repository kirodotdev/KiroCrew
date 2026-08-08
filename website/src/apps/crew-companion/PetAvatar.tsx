/**
 * PetAvatar — render the CURRENT pet, at any size, inside arbitrary UI.
 *
 * Why this exists: before it, every surface that wanted to show the pet outside
 * the desktop overlay resolved the art itself. `ChatLoadingPet` hand-rolled its
 * own resolution and, in doing so, silently dropped colour-map support — so a
 * user who recoloured their pet saw the uncustomised one in the chat. Adding a
 * third copy for the panel would have compounded that drift, so resolution lives
 * here once and every surface consumes it.
 *
 * Covers all three pack formats (svg / lottie / sprite), applies the active
 * colour map, and keeps itself current when the user switches pack or recolours.
 *
 * The eye overlay is added ONLY for the built-in pack, whose bodies are drawn
 * eyeless on purpose. Custom packs bake their own eyes in, so overlaying would
 * double them up.
 */
import React, { useEffect, useState } from 'react'
import { GhostEyeOverlay } from './GhostEyeOverlay'
import GhostAccessoryLayer from './GhostAccessoryLayer'
import { HIDES_EYES, type GhostAccessory } from './ghostAccessories'
import { ghostPoseForKey, ghostEyeOffsetFor, POSED_ANIMS } from './ghostEyes'
import { animClassFor, type PetAnim } from './petAnim'
import { LottieRenderer } from './LottieRenderer'
import { SpriteRenderer } from './SpriteRenderer'
import { toDataUri } from './animationResolver'
import { applySvgColorMap, type ColorMap } from './colorCustomizer'
import './petMotion.css'
import ghostIdleUrl from './assets/kiro_idle.svg'
import { petBridge } from './petBridge'

// Same method names as the desktop app's IPC bridge, over Kiro Crew's gateway.
const api = petBridge

/** The built-in pack id. Only this one gets the live eye overlay. */
const DEFAULT_PACK = 'kiro-ghost'

/**
 * What the pet is reacting to. Drives BOTH the pack slot (which art) and the
 * motion (how it moves), because the two always travel together — a celebration
 * that used the error art would be nonsense.
 */
export type PetState =
  | 'idle' | 'loading' | 'done' | 'error'
  // Guided-breathing phases. Optional in a pack; each falls back to idle.
  | 'inhale' | 'hold' | 'exhale'

type PackSlot = 'idle' | 'loading' | 'done' | 'inhale' | 'hold' | 'exhale'

const STATE_TO_SLOT: Record<PetState, PackSlot> = {
  idle: 'idle',
  loading: 'loading',
  done: 'done',
  // Packs are not required to ship error art; the resolver falls back to idle
  // and the shake carries the meaning.
  error: 'idle',
  inhale: 'inhale',
  hold: 'hold',
  exhale: 'exhale',
}

/** Slots whose only sensible fallback is idle — never the busy aliases. */
const BREATHING_SLOTS = new Set<PackSlot>(['inhale', 'hold', 'exhale'])

/**
 * The motion a bare state implies, for surfaces that just hand us a state (the
 * gallery, the chat loader). The live pet passes an explicit `anim` instead, because
 * only it knows about walking, mood and docking — see petAnim.activeAnimFor.
 */
const STATE_TO_ANIM: Record<PetState, PetAnim> = {
  idle: null,
  loading: 'ponder-loop',
  done: 'celebrate',
  error: 'error',
  // No keyframe of our own during breathing: the overlay drives the scale, and a
  // second animation would fight the phase timing it is trying to express.
  inhale: null,
  hold: null,
  exhale: null,
}

type Art =
  | { kind: 'default' }
  | { kind: 'svg'; uri: string }
  | { kind: 'lottie'; data: string }
  | { kind: 'sprite'; src: string; frameWidth: number; frameHeight: number; fps: number }

export interface PetAvatarProps {
  /** Rendered box size in px (square). */
  size: number
  /**
   * What the pet is reacting to. `idle` is still, `loading` ponders on a loop,
   * `done` hops once, `error` shakes once. Art and motion are chosen together.
   */
  state?: PetState
  /**
   * Current mood, which outranks state for the eyes — matching ghostPoseFor.
   * A notification sets a mood, and that is most of what makes the companion
   * visibly react.
   */
  mood?: string
  /**
   * The dress-up prop to wear, if any.
   *
   * Passed in rather than read here, because two sources feed it and only the
   * caller can arbitrate: the appearance the user picked in the gallery, and a
   * transient celebrate prop that overlays it for the length of a hop. Reading
   * config here would let the two disagree.
   */
  accessory?: GhostAccessory
  /**
   * Docked at a screen edge. The desktop app goes QUIET when docked — it drops to
   * the neutral 'primary' pose and suppresses the error / curious / walking
   * reactions — rather than adopting a separate docked pose. GHOST_EYE_MAP has a
   * 'docked' entry, but nothing in the source ever selects it.
   */
  docked?: boolean
  /** True while an ancestor mirrors the art; the eyes invert their gaze to match. */
  flipX?: boolean
  /** Eye offsets in eye-span units, for the built-in pack only. */
  eyeDx?: number
  eyeDy?: number
  /**
   * Let the built-in ghost's eyes follow the cursor. Only the live desktop pet
   * turns this on; static previews (gallery, chat loader) leave the eyes still.
   */
  trackCursor?: boolean
  /**
   * The motion to play, overriding the one `state` alone implies. The live pet passes
   * this because the choice depends on things a state cannot express — whether the
   * companion is travelling, curious, or tucked against an edge. Pass `null` to hold
   * still; omit it to let the state decide.
   */
  anim?: PetAnim
  /**
   * A monotonically increasing "a fresh reaction just happened" counter.
   *
   * The animated span is keyed off the motion NAME so a change of motion restarts the
   * keyframes. But a reaction can repeat WITHOUT the name changing — two completions in
   * a row are both `celebrate`, and because a `happy` mood also resolves to `celebrate`
   * the companion is often already in celebrate-continuity when the next finish lands.
   * With a name-only key React reuses the same DOM node, the CSS animation is never
   * re-triggered, and the hop is silently skipped. Folding this counter into the key
   * forces a remount on every fresh reaction, so each celebration actually hops (and
   * each error actually shakes) even when the previous one was the same motion.
   *
   * The live pet bumps it once per discrete reaction; static previews leave it at 0.
   */
  animEpoch?: number
  /**
   * Play a specific author-named "random" clip from the ACTIVE custom pack, instead
   * of the state slot's art. This is the channel PetDex extras (wave / waiting / run)
   * were missing: they were imported, written to disk under `random`, listed by
   * `randomNames` — and nothing could render them, because this component resolved
   * art by state slot only. Ignored for the built-in pack, whose idle life is the
   * fidget pool. Cleared (undefined) means "render the state slot as always".
   */
  clipName?: string
  className?: string
}

export const PetAvatar: React.FC<PetAvatarProps> = ({
  size, state = 'idle', mood, docked = false, eyeDx = 0, eyeDy = 0, trackCursor = false, flipX = false, anim, animEpoch = 0, clipName, className,
  accessory = 'none',
}) => {
  /**
   * Which eye pose to draw.
   *
   * Mirrors ghostPoseFor's precedence (mood outranks state) but goes through
   * ghostPoseForKey, because this component's `state` is the DISPLAY union
   * (idle / loading / done / error / breathing phases) while ghostPoseFor expects the
   * pack-authoring union. ghostPoseForKey looks a bare key up in both tables and falls
   * back to 'primary', so a state with no pose of its own is safe rather than a crash.
   */
  const eyePose = docked
    ? 'primary'
    : ghostPoseForKey(mood && mood !== 'neutral' ? mood : state)

  const slot = STATE_TO_SLOT[state]
  /**
   * An explicit `anim` wins — including an explicit `null`, which means "hold still"
   * and must not fall back to the state's own motion. `undefined` means the caller
   * has no opinion, so the state decides.
   */
  const animName: PetAnim = anim !== undefined ? anim : STATE_TO_ANIM[state]
  /**
   * A posed reaction holds the eyes where the footage puts them: live cursor gaze
   * would fight the pose and read as a twitch. The offset is ART-relative, so it is
   * added to whatever the caller asked for rather than replacing it.
   */
  const posed = POSED_ANIMS.has(animName ?? '')
  const eyeOff = ghostEyeOffsetFor(animName)
  const [art, setArt] = useState<Art>({ kind: 'default' })
  // Built-in pack only: the user's recolour of the default ghost.
  const [colorMap, setColorMap] = useState<ColorMap | null>(null)
  // Bumped to force a re-resolve when the pack or colours change underneath us.
  const [rev, setRev] = useState(0)

  // Keep up with pack switches and recolours. Both were missing from the chat
  // loader, which resolved once on mount and then went stale.
  useEffect(() => {
    const offPack = api?.onGalleryActiveChanged?.(() => setRev(n => n + 1))
    const offColor = api?.onColorMapChanged?.((d: { packId: string; colorMap: ColorMap }) => {
      if (d?.packId === DEFAULT_PACK) setColorMap(d.colorMap)
      setRev(n => n + 1)
    })
    return () => { offPack?.(); offColor?.() }
  }, [])

  useEffect(() => {
    let alive = true

    ;(async () => {
      const cfg = await api?.getCrewCompanionConfig?.().catch(() => null)
      const packId: string = cfg?.activeAppearance || DEFAULT_PACK

      if (packId === DEFAULT_PACK) {
        // Built-in ghost: raw SVG + the user's colour map, if any.
        const cm = await api?.presetsGetColorMap?.(DEFAULT_PACK).catch(() => null)
        if (!alive) return
        setColorMap(cm && Object.keys(cm).length > 0 ? cm : null)
        setArt({ kind: 'default' })
        return
      }

      const detail = await api?.galleryGetPackDetail?.(packId).catch(() => null)
      if (!alive || !detail?.animations) return

      // Requested slot first, then the legacy busy aliases, then idle. `thinking`
      // and `working` mean the same thing as `loading` for packs authored before
      // the status/random split.
      //
      // Breathing phases skip the busy aliases: a pack with no `inhale` should show
      // its calm idle body, not the art it drew for "working".
      const a = detail.animations
      /*
       * A requested clip outranks the state slot — it IS the reason we are
       * rendering. Falling back to idle when the clip is missing (a stale name
       * after a pack edit) keeps the companion visible rather than blank.
       */
      const entry = clipName
        ? (a[clipName] || a.idle)
        : BREATHING_SLOTS.has(slot)
          ? (a[slot] || a.idle)
          : (a[slot] || a.loading || a.thinking || a.working || a.idle)
      if (!entry) return

      const content = typeof entry === 'string' ? entry : entry.content
      const format = typeof entry === 'string' ? 'svg' : entry.format
      if (!content) return

      if (format === 'lottie') {
        setArt({ kind: 'lottie', data: content })
      } else if (format === 'sprite') {
        const sp = detail.sprite || {}
        setArt({
          kind: 'sprite',
          src: content.startsWith('data:') ? content : `data:image/png;base64,${content}`,
          frameWidth: sp.frameWidth || 32,
          frameHeight: sp.frameHeight || 32,
          fps: sp.fps || 6,
        })
      } else {
        setArt({ kind: 'svg', uri: toDataUri(content) })
      }
    })()

    return () => { alive = false }
  }, [slot, clipName, rev])

  const isDefault = art.kind === 'default'

  // The built-in SVG is fetched as a URL by the bundler, so a colour map has to
  // be applied to its text. Without a map we can use the URL directly.
  const [defaultUri, setDefaultUri] = useState<string | null>(null)
  useEffect(() => {
    if (!isDefault || !colorMap) { setDefaultUri(null); return }
    let alive = true
    fetch(ghostIdleUrl)
      .then(r => r.text())
      .then(raw => { if (alive) setDefaultUri(toDataUri(applySvgColorMap(raw, colorMap))) })
      .catch(() => {})
    return () => { alive = false }
  }, [isDefault, colorMap])

  const imgSrc = art.kind === 'svg' ? art.uri : (defaultUri ?? ghostIdleUrl)

  return (
    <span
      className={className}
      style={{ display: 'inline-flex', lineHeight: 0 }}
      aria-hidden="true"
    >
      {/* Motions are keyed off the resolved animation AND a per-reaction epoch, so a
          fresh reaction restarts the keyframes instead of inheriting a half-played one
          — even when it repeats the SAME motion (a second celebrate hop, a second
          error shake), which a name-only key silently swallowed. */}
      <span
        key={`${animName ?? state}#${animEpoch}`}
        className={animClassFor(animName)}
        style={{ position: 'relative', width: size, height: size, display: 'block' }}
      >
        {art.kind === 'lottie' ? (
          <LottieRenderer animationData={art.data} width={size} height={size} />
        ) : art.kind === 'sprite' ? (
          <SpriteRenderer
            src={art.src}
            frameWidth={art.frameWidth}
            frameHeight={art.frameHeight}
            fps={art.fps}
            displaySize={size}
          />
        ) : (
          <img
            src={imgSrc}
            alt=""
            style={{ width: size, height: size, objectFit: 'contain', display: 'block' }}
          />
        )}
        {/*
          Built-in bodies are eyeless by design; custom art is not.

          A prop that sits OVER the eyes suppresses them — shades, a sleep mask and
          the retired antenna. Drawing both leaves pupils floating on top of the
          lenses, which is why the source keeps one source of truth for the worn
          prop and consults it here.
        */}
        {isDefault && !HIDES_EYES.has(accessory) && (
                  <GhostEyeOverlay
                    pose={eyePose}
                    size={size}
                    dx={eyeDx + eyeOff.dx}
                    dy={eyeDy + eyeOff.dy}
                    track={trackCursor && !posed}
                    flipX={flipX}
                    anim={animName}
                    posed={posed}
                  />
                )}
        {/*
          Props ride the same layered container, so they move with every motion —
          but ONLY on the built-in ghost. Their placement is derived from
          GHOST_EYE_MAP (the built-in ghost's eye geometry), so on a custom pack a hat
          or shades land by a face that is not the pack's own — floating in empty space
          beside a capybara. The eye overlay is already `isDefault`-gated for the same
          reason; the accessory layer must be too. A custom pack simply celebrates with
          its hop and no prop, which is the honest degrade: we cannot know where an
          arbitrary custom sprite wears a hat.
        */}
        {isDefault && (
          <GhostAccessoryLayer id={accessory} pose={eyePose} flipX={flipX} />
        )}
      </span>
    </span>
  )
}
