/**
 * Mochi - Pet widget with SVG animation + inline bubble
 * Per-display overlay model: each display has its own overlay window.
 * The pet lives in exactly one overlay at a time. Drag to edge → transfer to adjacent display.
 * Drag is pure JS within the overlay — no setPosition IPC, so it's silky smooth.
 */
import React, { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import { i18nT } from '../../../../i18n/t'
import type { PetState, PetMood } from '../shared/types'
import type {
  MoodAnimationMap,
  PackManifest,
  PackMeta,
  SpriteConfig,
  StateAnimationMap,
} from '../shared/appearanceTypes'
import { BUBBLE_COLORS, applyThemeVarsOnly, type ThemeId } from '../shared/themes'
import { useDisplayActivation } from './hooks/useDisplayActivation'
import { useMood } from './hooks/useMood'
import { useApprovalBubble } from './hooks/useApprovalBubble'
import { useBubble } from './hooks/useBubble'
import { useDrag } from './hooks/useDrag'
import { useEdgeHide } from './hooks/useEdgeHide'
import { useWalking } from './hooks/useWalking'
import { useMouseForward } from './hooks/useMouseForward'
import { peekNudgeFor } from './peekNudge'
import { AnimationResolver, toDataUri } from './animationResolver'
import { applySvgColorMap, type ColorMap } from '../shared/colorCustomizer'
import {
  BUILTIN_MOCHI_ID,
  DEFAULT_PET_NAME,
  resolveActivePackId,
  resolvePetName,
} from '../../builtinPacks'
import { LottieRenderer } from './LottieRenderer'
import { SpriteRenderer } from './SpriteRenderer'
import { PetContextMenu } from './PetContextMenu'

// ── Hardcoded SVG fallbacks for default-mochi built-in pack ────────────────
// These are compiled in via Vite ?raw — zero I/O, instant render on startup.
import svgIdleRaw from '../../assets/animations/mochi_idle.svg?raw'
import svgWalkingRaw from '../../assets/animations/mochi_walking.svg?raw'
import svgPeekRaw from '../../assets/animations/mochi_peek.svg?raw'
import svgErrorRaw from '../../assets/animations/mochi_error.svg?raw'
import svgThinkingRaw from '../../assets/animations/mochi_thinking.svg?raw'
import svgWorkingRaw from '../../assets/animations/mochi_working.svg?raw'
import svgHappyRaw from '../../assets/animations/mochi_done.svg?raw'
import svgSleepyRaw from '../../assets/animations/mochi_sleeping.svg?raw'
import svgPeekThinkingRaw from '../../assets/animations/mochi_peek_thinking.svg?raw'

// Raw SVG strings kept as source data for dynamic color replacement.
// Converted to data URIs at runtime via useMemo (see fallbackUriCache).
const fallbackStateRaw: Record<PetState, string> = {
  idle: svgIdleRaw, thinking: svgThinkingRaw, working: svgWorkingRaw,
  walking: svgWalkingRaw, error: svgErrorRaw, offline: svgSleepyRaw,
}
const fallbackMoodRaw: Record<PetMood, string | null> = {
  neutral: null, happy: svgHappyRaw, sleepy: svgSleepyRaw,
  curious: svgThinkingRaw, busy: svgWorkingRaw, scared: svgErrorRaw,
}

/** Apply colorMap to raw SVG and convert to data URI */
function colorize(raw: string, cm: ColorMap | null): string {
  return toDataUri(cm && Object.keys(cm).length > 0 ? applySvgColorMap(raw, cm) : raw)
}

import { PET_W, PET_H, BUBBLE_W } from '../shared/constants'
import { BUBBLE_LAYOUT_DEFAULTS } from '../shared/bubbleLayout'

import { api, type InlineAnimation } from '../mochiApi'

/**
 * The live appearance-switch payload.
 *
 * The bridge declares it as an open record because it merges two shapes: `{ packId }`
 * alone for the built-in cat (whose art is compiled in here), and a flattened
 * `PackDetail` for every other pack. The fields the pet reads are named here so a
 * missing guard is a compile error rather than a silent `undefined`.
 */
type ActivePackChange = {
  packId?: string
  meta?: PackMeta
  animations?: Record<string, string | InlineAnimation>
  sprite?: SpriteConfig
  flipX?: boolean
}

/**
 * Builds an AnimationResolver from pack detail data returned by IPC.
 * The data shape is { meta, animations } where animations is a Record<string, string>
 * mapping state/mood names (e.g. 'idle', 'happy') to raw animation content.
 * We reconstruct the manifest and build a contentMap keyed by filenames.
 */
function buildResolverFromPackDetail(data: {
  // Both optional, because the guard below is what the two call sites rely on:
  // the live-switch payload carries neither key for the built-in cat.
  meta?: PackMeta
  animations?: Record<string, string | InlineAnimation>
}): AnimationResolver | null {
  const { meta, animations } = data
  if (!meta || !animations) return null

  const states: Partial<StateAnimationMap> = {}
  const moods: MoodAnimationMap = {}
  const contentMap: Record<string, string> = {}

  const requiredStates = ['idle', 'walking', 'thinking', 'working', 'error', 'offline']
  const optionalStates = ['peeking', 'peekThinking']
  const moodNames = ['happy', 'sleepy', 'curious', 'busy', 'scared']

  for (const key of Object.keys(animations)) {
    const val = animations[key]
    const content = typeof val === 'string' ? val : val.content
    const fmt = typeof val === 'string'
      ? (meta.format === 'lottie' ? 'lottie' : meta.format === 'sprite' ? 'sprite' : 'svg')
      : val.format
    const ext = fmt === 'lottie' ? '.json' : fmt === 'sprite' ? '.png' : '.svg'
    const filename = `${key}${ext}`
    contentMap[filename] = content
    if (requiredStates.includes(key) || optionalStates.includes(key)) {
      states[key as keyof StateAnimationMap] = filename
    } else if (moodNames.includes(key)) {
      moods[key as keyof MoodAnimationMap] = filename
    }
  }

  // `idle` is the ONLY slot a pack must assign — the same contract the store
  // enforces (appearance_store.REQUIRED_STATES) and the resolver now honours
  // (every unassigned slot falls back to idle). Demanding all six here rejected
  // packs the store had happily saved, and both call sites do
  // `if (resolver) set…` — so an "incomplete" pack left the CAT on screen. That
  // is the same wrong answer for "the pack is unusable" and "the pack only ships
  // one drawing", and the second is a supported pack.
  if (!states['idle']) {
    // eslint-disable-next-line no-console
    console.error('[mochi] pack cannot drive the pet — no idle art', {
      packId: meta?.id,
      slotsProvided: Object.keys(animations),
    })
    return null
  }

  const manifest: PackManifest = {
    meta,
    // Partial BY DESIGN, which is why the accumulator above is a `Partial` and only
    // this one line asserts it complete: `idle` is the sole required slot (see the
    // guard above) and the resolver falls back to it for every slot a pack leaves
    // out, so padding the map with art the pack does not ship would be the lie.
    states: states as StateAnimationMap,
    moods,
  }

  return new AnimationResolver(manifest, contentMap)
}

/** Bubble overlay — measures its own width so the tail and position are always correct */
const BubbleOverlay: React.FC<{
  text: string; bubbleY: number; bubbleAbove: boolean
  bubbleFading: boolean; petCenterX: number; themeId: ThemeId; onDismiss: () => void
  onHeightMeasured?: (h: number) => void
}> = ({ text, bubbleY, bubbleAbove, bubbleFading, petCenterX, themeId, onDismiss, onHeightMeasured }) => {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [measuredW, setMeasuredW] = useState(0)
  // A retired theme id from stored settings (e.g. pre-consolidation 'mocha')
  // must fall back, not crash: bc.bg on undefined unmounted the entire pet
  // tree on the first bubble render.
  const bc = BUBBLE_COLORS[themeId] ?? BUBBLE_COLORS.kirocrew

  // Deliberately dependency-LESS: the bubble carries no ResizeObserver, so
  // re-measuring on every commit is the only thing that notices a text or layout
  // change (the tail and the clamp are computed from the measured width). The
  // update chain still terminates in one extra commit — `w !== measuredW` stops
  // the local write, and the parent's `onHeightMeasured` setter bails out on an
  // identical height.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- measure-after-every-render, see above: a dep list would freeze the measurement at the deps' last change and mis-place the tail for every later text change.
  useEffect(() => {
    if (wrapRef.current) {
      const w = wrapRef.current.offsetWidth
      const h = wrapRef.current.offsetHeight
      if (w !== measuredW) setMeasuredW(w)
      onHeightMeasured?.(h)
    }
  })

  // Re-center: adjust left so bubble is centered on petCenterX using actual width
  const actualW = measuredW || BUBBLE_W
  const margin = BUBBLE_LAYOUT_DEFAULTS.margin
  const centeredLeft = petCenterX - actualW / 2
  const clampedLeft = Math.max(margin, Math.min(window.innerWidth - actualW - margin, centeredLeft))

  // Tail X: point toward pet center, clamped inside actual bubble width
  const PAD = 20
  const tailRaw = petCenterX - clampedLeft
  const tailX = Math.max(PAD, Math.min(actualW - PAD, tailRaw))

  const tail = (dir: 'up' | 'down') => (
    <div style={{ position: 'relative', height: 8 }}>
      <div style={{
        position: 'absolute', left: tailX - 6,
        width: 0, height: 0,
        borderLeft: '6px solid transparent', borderRight: '6px solid transparent',
        ...(dir === 'down'
          ? { borderBottom: `8px solid ${bc.bg}` }
          : { borderTop: `8px solid ${bc.bg}` }),
      }} />
    </div>
  )

  return (
    <div ref={wrapRef} style={{
      position: 'absolute', left: clampedLeft, top: bubbleY,
      maxWidth: BUBBLE_W, width: 'fit-content',
      zIndex: 10, cursor: 'pointer',
      animation: bubbleFading ? 'fadeOut 0.3s ease-in forwards' : 'fadeIn 0.3s ease-out',
    }}
      // The bubble dismisses on click, which makes it a control: role plus a name
      // is what a screen reader needs to announce it as dismissible, and Enter or
      // Space runs the SAME dismiss the click does. Focus is real rather than
      // decorative — this page also runs as a plain browser tab (the pet's dev
      // preview), where it is reachable by keyboard; inside the transparent
      // always-on-top overlay the key handler is simply never reached.
      role="button"
      tabIndex={0}
      aria-label={i18nT('apps.mochi.petWidget.dismiss_bubble')}
      onClick={onDismiss}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onDismiss() }
      }}
    >
      {!bubbleAbove && tail('down')}
      <div style={{
        background: bc.bg, borderRadius: 14, padding: '10px 14px',
        color: bc.text, font: '13px/1.5 -apple-system, BlinkMacSystemFont, sans-serif',
        wordBreak: 'break-word', boxShadow: `0 4px 16px ${bc.shadow}`,
        WebkitAppRegion: 'no-drag',
      } as React.CSSProperties}>{text}</div>
      {bubbleAbove && tail('up')}
    </div>
  )
}

export const PetWidget: React.FC = () => {
  const petDivRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<PetState>('idle')
  const [displayState, setDisplayState] = useState<PetState>('idle')
  const [opacity, setOpacity] = useState(1)
  const clearMoodRef = useRef<() => void>(() => {})
  const isPeekingForSvgRef = useRef(false)
  // Last logged render shape, so the diagnostic below fires on CHANGE only
  // (this block runs on every frame of a walk).
  const lastRenderShapeRef = useRef('')
  // Theme ids collapsed to one when overall themes were removed; the stored
  // value is still read, so the state stays a plain string.
  const [themeId, setThemeId] = useState<string>('kirocrew')

  // AnimationResolver state — null means using hardcoded fallbacks
  const [resolver, setResolver] = useState<AnimationResolver | null>(null)
  const [spriteConfig, setSpriteConfig] = useState<SpriteConfig | null>(null)
  // The PACK's baseline facing, XOR'd with situational flips in the transform
  // below. Read from the pack-level `flipX` (falling back to a sprite sheet's
  // own flag), so a mirrored SVG/Lottie pack — the built-in Kiro Ghost — can say
  // so without pretending to be a sprite.
  const [packFlipX, setPackFlipX] = useState(false)

  // Color customization for default-mochi fallback SVGs
  const [mochiColorMap, setMochiColorMap] = useState<ColorMap | null>(null)

  // Cached fallback data URIs — recomputed only when colorMap changes
  const fallbackUriCache = useMemo(() => {
    const stateCache: Record<string, string> = {}
    for (const [k, raw] of Object.entries(fallbackStateRaw)) {
      stateCache[k] = colorize(raw, mochiColorMap)
    }
    const moodCache: Record<string, string | null> = { neutral: null }
    for (const [k, raw] of Object.entries(fallbackMoodRaw)) {
      if (k === 'neutral' || !raw) continue
      moodCache[k] = colorize(raw, mochiColorMap)
    }
    return {
      state: stateCache as Record<PetState, string>,
      mood: moodCache as Record<PetMood, string | null>,
      peek: colorize(svgPeekRaw, mochiColorMap),
      peekThinking: colorize(svgPeekThinkingRaw, mochiColorMap),
    }
  }, [mochiColorMap])

  // Edge hide/peek state
  const { hideEdge, isPeeking, setIsPeeking, setHideEdge, isPeekingRef } = useEdgeHide(isPeekingForSvgRef)

  // Context menu state
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null)

  // Mood (must be before useDrag since clearPersistentMood is passed as option)
  const { moodRef, clearPersistentMood } = useMood()
  clearMoodRef.current = clearPersistentMood

  // Drag
  const { pos, setPos, onMouseDown, dragging, dragPollingStarted, posReady } = useDrag(
    { x: -999, y: -999 },
    { clearPersistentMood, displayState, setDisplayState, isPeekingRef, setIsPeeking, setHideEdge }
  )

  // Per-display overlay activation
  const { isActive, isActiveRef } =
    useDisplayActivation({
      onActivate: (x, y, isDragging) => {
        setPos({ x, y })
        if (isDragging) {
          dragging.current = true
          dragPollingStarted.current = true
        }
      },
      onDeactivate: () => {
        dragging.current = false
        dragPollingStarted.current = false
      },
    })

  // ADDED (not upstream): the tooltip names the PET, which the user can
  // rename in Settings. Upstream hard-coded the product name here.
  // Declared before the bubble hooks: the approval bubble speaks as the pet and
  // takes this name.
  const [petName, setPetName] = useState(DEFAULT_PET_NAME)

  // Bubble transport (text, fade, auto-dismiss).
  const { bubble, bubbleFading, dismissBubble, showBubble } = useBubble()
  // Approval policy: a pending approval blocks the agent, and its card lives in
  // the panel — which is usually closed while the pet is on the desktop. Raises
  // a sticky bubble through the SAME path a backend `notify` would use.
  useApprovalBubble(petName, showBubble, dismissBubble)

  // Load initial pet state, theme config, and active appearance pack
  useEffect(() => {
    api?.getPetState?.().then((s: PetState) => { if (s) setState(s) })
    api?.getMochiConfig?.().then((c) => {
      // `theme` sits OUTSIDE the pet-facing subset `getMochiConfig` declares — it is
      // a stored key the pet honours when the settings still carry one — so it is
      // read off the raw record rather than asserted onto the config type.
      const storedTheme = (c as Record<string, unknown> | undefined)?.theme
      if (typeof storedTheme === 'string' && storedTheme) {
        setThemeId(storedTheme)
        applyThemeVarsOnly(storedTheme as ThemeId)
      }
      setPetName(resolvePetName(c))
      // Only fetch pack detail for non-cat packs: default-mochi uses hardcoded
      // SVG fallbacks (compiled in via Vite ?raw) so it can be recoloured.
      // The id comes from `avatar` when no custom pack overrides it -- reading
      // activeAppearance alone rendered the cat for a user who chose the ghost.
      const packId = resolveActivePackId(c)
      if (packId === BUILTIN_MOCHI_ID) {
        // Load saved colorMap for default-mochi
        api?.presetsGetColorMap?.('default-mochi').then((cm) => {
          if (cm && Object.keys(cm).length > 0) setMochiColorMap(cm)
        }).catch(() => {})
      } else {
        api?.galleryGetPackDetail?.(packId).then((data) => {
          if (data?.meta && data?.animations) {
            const newResolver = buildResolverFromPackDetail(data)
            if (newResolver) setResolver(newResolver)
            setSpriteConfig(data.sprite || null)
            setPackFlipX((data.flipX ?? data.sprite?.flipX) === true)
          } else {
            // The pack the settings point at produced no art. Silence here read
            // as "the ghost just doesn't work" — say which pack and what came
            // back instead.
            // eslint-disable-next-line no-console
            console.error('[mochi] active pack returned no usable detail', {
              packId,
              hasMeta: Boolean(data?.meta),
              hasAnimations: Boolean(data?.animations),
            })
          }
        }).catch((err: unknown) => {
          // eslint-disable-next-line no-console
          console.error('[mochi] could not load the active pack', packId, err)
        })
      }
    })
  }, [])

  // Keep theme + language in sync with config changes
  useEffect(() => {
    const off = api?.onThemeChanged?.((id: string) => { setThemeId(id as ThemeId); applyThemeVarsOnly(id as ThemeId) })
    // The bridge publishes the whole settings record, of which the pet reads one key.
    const offCfg = api?.onConfigUpdated?.((m: { theme?: string }) => {
      if (m?.theme) { setThemeId(m.theme); applyThemeVarsOnly(m.theme as ThemeId) }
    })
    const offColor = api?.onColorMapChanged?.((data: { packId: string; colorMap: ColorMap }) => {
      if (data.packId === 'default-mochi') {
        const cm = data.colorMap && Object.keys(data.colorMap).length > 0 ? data.colorMap : null
        setMochiColorMap(cm)
        // Also update resolver if it's active for default-mochi
        if (resolver) resolver.setColorMap(cm)
      }
    })
    return () => { off?.(); offCfg?.(); offColor?.() }
  }, [resolver])

  // Task 9.3: Listen to gallery:active-changed broadcast for pack switching

  // Cross-display drag: main process asks all overlays to listen for mouseup
  useEffect(() => {
    const off = api?.onDragListenMouseup?.(() => {
      const handler = () => {
        api?.dragMouseup?.()
        window.removeEventListener('mouseup', handler, true)
      }
      window.addEventListener('mouseup', handler, true)
    })
    return () => { off?.() }
  }, [])

  useEffect(() => {
    const off = api?.onGalleryActiveChanged?.((data: ActivePackChange) => {
      if (data?.packId === 'default-mochi') {
        setOpacity(0)
        setTimeout(() => { setResolver(null); setSpriteConfig(null); setPackFlipX(false); setOpacity(1) }, 150)
        // Restore saved colorMap for default-mochi
        api?.presetsGetColorMap?.('default-mochi').then((cm) => {
          setMochiColorMap(cm && Object.keys(cm).length > 0 ? cm : null)
        }).catch(() => {})
        return
      }
      // Switching away from default-mochi — clear colorMap
      setMochiColorMap(null)
      if (data?.meta && data?.animations) {
        const newResolver = buildResolverFromPackDetail(data)
        if (newResolver) {
          setOpacity(0)
          setTimeout(() => {
            setResolver(newResolver)
            setSpriteConfig(data.sprite || null)
            setPackFlipX((data.flipX ?? data.sprite?.flipX) === true)
            setOpacity(1)
          }, 150)
        }
      } else {
        // The mount path logs this; the LIVE path did not, which is why "I
        // clicked Apply and the pet stayed a cat" left no trace anywhere.
        // eslint-disable-next-line no-console
        console.error('[mochi] live appearance switch carried no usable art', {
          packId: data?.packId,
          hasMeta: Boolean(data?.meta),
          hasAnimations: Boolean(data?.animations),
        })
      }
    })
    return () => off?.()
  }, [])

  // Pet state changes — with minimum display duration to avoid flicker
  const MIN_DISPLAY_MS = 2000
  const prevStateRef = useRef<PetState>(state)
  const displayLockedUntil = useRef(0)
  const queuedState = useRef<PetState | null>(null)
  const queueTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const resolveSvg = useCallback((s: PetState) => {
    // Peek: cached fallback SVGs (only reached when no resolver/pack)
    if (isPeekingForSvgRef.current) {
      if (s === 'thinking' || s === 'working') return fallbackUriCache.peekThinking
      if (s === 'idle' || s === 'offline') return fallbackUriCache.peek
    }
    if (s === 'walking') {
      // Use resolver for walking if available, else fallback
      if (resolver) {
        const source = resolver.resolve(s, 'neutral')
        if (source.format === 'svg') return source.uri
      }
      return fallbackUriCache.state.walking
    }
    // Use resolver when available
    if (resolver) {
      const source = resolver.resolve(s, moodRef.current)
      return source.uri
    }
    // Fallback to cached default-mochi SVGs (with colorMap applied)
    if (moodRef.current !== 'neutral') return fallbackUriCache.mood[moodRef.current] || fallbackUriCache.state.idle
    return fallbackUriCache.state[s] || fallbackUriCache.state.idle
  }, [resolver, fallbackUriCache, moodRef])

  const applyDisplayState = useCallback((s: PetState) => {
    const oldSvg = resolveSvg(displayState)
    const newSvg = resolveSvg(s)
    if (oldSvg !== newSvg) {
      setOpacity(0)
      setTimeout(() => { setDisplayState(s); setOpacity(1) }, 150)
    } else {
      setDisplayState(s)
    }
    if (s !== 'idle' && s !== 'walking') displayLockedUntil.current = Date.now() + MIN_DISPLAY_MS
  }, [displayState, resolveSvg])
  const applyRef = useRef(applyDisplayState)
  applyRef.current = applyDisplayState

  useEffect(() => {
    const off = api?.onStateChange?.((s: PetState) => {
      if (prevStateRef.current === s) return
      if (s === 'thinking' || s === 'walking') clearMoodRef.current()
      prevStateRef.current = s
      setState(s)
      const now = Date.now()
      if (queueTimer.current) { clearTimeout(queueTimer.current); queueTimer.current = null }
      if (now >= displayLockedUntil.current) {
        applyRef.current(s)
      } else {
        queuedState.current = s
        const delay = displayLockedUntil.current - now
        queueTimer.current = setTimeout(() => {
          const next = queuedState.current
          queuedState.current = null; queueTimer.current = null
          if (next !== null) applyRef.current(next)
        }, delay)
      }
    })
    return () => { off?.(); if (queueTimer.current) clearTimeout(queueTimer.current) }
  }, [])

  // Walking — onWalkEnd handles edge state updates and position persistence
  const handleWalkEnd = useCallback((finalPos: { x: number; y: number }) => {
    const edgeThreshold = 40
    const atLeft = finalPos.x <= edgeThreshold
    const atRight = finalPos.x >= window.innerWidth - PET_W - edgeThreshold
    if (atLeft || atRight) {
      setHideEdge(atLeft ? 'left' : 'right')
      setIsPeeking(true)
    } else {
      setIsPeeking(false)
      setHideEdge(null)
    }
    api?.savePosition?.(finalPos.x, finalPos.y)
    api?.walkDone?.()
  }, [setIsPeeking, setHideEdge])

  const { isWalking, walkDir, walkTilt, cancelWalk } = useWalking(
    pos, setPos, handleWalkEnd, setIsPeeking, setHideEdge
  )

  // Track visual position for bubble placement during walks
  // With rAF-based walk, pos always equals visual position
  //
  // …EXCEPT while peeking with a pack that has no peek art. A peek pose is a
  // specific half-off-screen DRAWING, and only the built-in cat ships one; the
  // ghost deliberately omits it (its four clips are all fully-visible floats), so
  // the resolver falls back to `idle` and the "peek" looked identical to standing
  // still at the edge. Sliding the art partly off-screen produces the read that
  // the missing drawing would have — the pet is tucked behind the edge, watching.
  //
  // Applied to visualPos rather than as a CSS transform on purpose: the speech
  // bubble anchors here and useMouseForward derives the click hitbox from it
  // (`petBox`), so a transform-only nudge would leave the pet clickable — and
  // talking — from where it no longer is.
  const peekNudge = peekNudgeFor({ isPeeking, hideEdge, resolver })
  const visualPos = peekNudge === 0 ? pos : { x: pos.x + peekNudge, y: pos.y }

  // Measured bubble height — updated by BubbleOverlay after render
  const [measuredBubbleH, setMeasuredBubbleH] = useState(0)

  // Smart bubble placement — computed once when bubble appears, then locked.
  // Bubble placement: simple, reliable, no jitter during drag.
  // Bubble always above pet, centered on pet, clamped to screen.
  const estimatedBubbleH = measuredBubbleH || 60
  const margin = BUBBLE_LAYOUT_DEFAULTS.margin
  const bubbleAbove = visualPos.y > estimatedBubbleH + margin + 20

  // Center bubble on pet center. Use petCenterX as anchor, not BUBBLE_W offset.
  // The bubble has width:fit-content (up to BUBBLE_W max), so we position its left edge
  // such that petCenterX falls roughly in the middle of the bubble.
  // We don't know actual width yet (BubbleOverlay measures it), so use BUBBLE_W as estimate
  // for the clamp, but the visual centering is handled by BubbleOverlay internally.
  const petCenterX = visualPos.x + PET_W / 2
  const rawBubbleX = petCenterX - BUBBLE_W / 2
  const bubbleX = Math.max(margin, Math.min(window.innerWidth - BUBBLE_W - margin, rawBubbleX))
  const bubbleY = bubbleAbove
    ? Math.max(margin, visualPos.y - estimatedBubbleH - 14)
    : Math.min(window.innerHeight - estimatedBubbleH - margin, visualPos.y + PET_H + 6)

  useMouseForward({
    pos, visualPos, bubble, bubbleX, bubbleAbove, bubbleY, bubbleHeight: measuredBubbleH,
    isPeekingForSvgRef, hideEdge, dragging, isActiveRef,
  })

  // Don't render anything if this overlay is not active
  if (!isActive) return null

  return (
    <>
      {/* Bubble */}
      {bubble && <BubbleOverlay
        text={bubble}
        bubbleY={bubbleY}
        bubbleAbove={bubbleAbove}
        bubbleFading={bubbleFading}
        petCenterX={visualPos.x + PET_W / 2}
        themeId={themeId as ThemeId}
        onDismiss={dismissBubble}
        onHeightMeasured={setMeasuredBubbleH}
      />}

      <div
        ref={petDivRef}
        style={{
          position: 'absolute', left: pos.x, top: pos.y,
          width: PET_W, height: PET_H,
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
          cursor: dragging.current ? 'grabbing' : 'grab',
          zIndex: 20,
          opacity: posReady ? 1 : 0,
          transition: 'transform 0.3s ease',
          transform: (() => {
            const parts: string[] = []
            // FIRST in the chain, so the mirror below cannot flip its direction:
            // `transform: translateX(40px) scaleX(-1)` mirrors the art in place
            // and then moves it +40px, whereas the reverse order would move it
            // -40px. Animated for free by the container's transform transition;
            // `left` is not usable here because the walk drives it every frame.
            if (peekNudge !== 0) {
              parts.push(`translateX(${peekNudge}px)`)
            }
            const onRightSide = pos.x > window.innerWidth / 2
            const shouldFlip = (isWalking && walkDir < 0) || (isPeeking && hideEdge === 'right') || (!isWalking && !isPeeking && onRightSide)
            // The pack's baseline facing. Was `spriteConfig?.flipX`, which only a
            // sprite sheet could set — so a mirrored Lottie pack had no way to
            // declare it and always rendered backwards.
            if (shouldFlip !== packFlipX) {
              parts.push('scaleX(-1)')
            }
            if (isWalking && walkTilt !== 0) {
              parts.push(`rotate(${walkTilt}deg)`)
            }
            return parts.length ? parts.join(' ') : 'none'
          })(),
        }}
        onMouseDown={(e) => { if (isWalking) cancelWalk(); onMouseDown(e) }}
        onDoubleClick={(e) => { e.preventDefault(); clearPersistentMood(); api?.openChat?.() }}
        onContextMenu={(e) => { e.preventDefault(); setCtxMenu({ x: e.clientX, y: e.clientY }) }}
        // The pet body is the widget's one control — press to drag, double-click to
        // open chat, right-click for the menu — so it carries a role, a name and a
        // tab stop. Enter and Space run the SAME activation the double-click does,
        // and the focus is real rather than decorative: this page also runs as a
        // plain browser tab (the pet's dev preview), where it is reachable by
        // keyboard; inside the transparent overlay window the handler is simply
        // never reached.
        role="button"
        tabIndex={0}
        aria-label={i18nT('apps.mochi.petWidget.open_chat', { name: petName })}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            clearPersistentMood()
            api?.openChat?.()
          }
        }}
      >
        {(() => {
          // Task 9.2: Determine if current animation is Lottie or SVG
          // Peek states always use hardcoded SVGs (not part of appearance packs)
          const isPeekState = isPeekingForSvgRef.current
          // Force walking animation when physically moving — overrides display lock
          const effectiveState = isWalking ? 'walking' as PetState : displayState
          // For peek states: use peeking/peekThinking if pack has them, else idle/thinking
          let currentSource: ReturnType<NonNullable<typeof resolver>['resolve']> | null = null
          if (resolver) {
            if (isPeekState) {
              const peekKey = (effectiveState === 'thinking' || effectiveState === 'working') ? 'peekThinking' : 'peeking'
              const fallbackKey = (effectiveState === 'thinking' || effectiveState === 'working') ? 'thinking' : 'idle'
              // `peeking`/`peekThinking` are pack SLOTS, not members of the state
              // machine, and the resolver handles both explicitly (see its own
              // `state as string` tests) while typing its parameter to the six
              // PetStates — so reaching that branch needs the widening bridge.
              currentSource = resolver.hasState(peekKey)
                ? resolver.resolve(peekKey as unknown as PetState, 'neutral')
                : resolver.resolve(fallbackKey, 'neutral')
            } else {
              currentSource = resolver.resolve(effectiveState, moodRef.current)
            }
          }
          const isLottie = currentSource?.format === 'lottie'
          const isSprite = currentSource?.format === 'sprite'

          // A pack that resolves to NOTHING looks identical to a pack that never
          // loaded and to a state with no art — all three reach this point and
          // render the fallback silently. The shape string is not logged (it
          // changes on every state and mood transition); it is only used to
          // report the one outcome that is a genuine fault, once per distinct
          // outcome rather than once per frame.
          const shape = `${resolver ? 'pack' : 'fallback'}:${effectiveState}:${moodRef.current}` +
            `:${currentSource?.format ?? 'svg-fallback'}:${currentSource?.uri.length ?? 0}`
          if (lastRenderShapeRef.current !== shape) {
            lastRenderShapeRef.current = shape
            if (resolver && (currentSource === null || currentSource.uri === '')) {
              // eslint-disable-next-line no-console
              console.warn('[mochi-pet] pack resolved NO art for', effectiveState)
            }
          }

          if (isSprite && currentSource) {
            return (
              <div style={{
                width: PET_W, height: PET_H, opacity, flexShrink: 0,
                transition: 'opacity 200ms ease-in-out', pointerEvents: 'none',
                transform: 'none',
              }}>
                <SpriteRenderer
                  src={currentSource.uri}
                  frameWidth={spriteConfig?.frameWidth || PET_W}
                  frameHeight={spriteConfig?.frameHeight || PET_H}
                  fps={spriteConfig?.fps || 8}
                  displaySize={PET_W}
                />
              </div>
            )
          }

          if (isLottie) {
            return (
              <div style={{
                width: PET_W, height: PET_H, opacity, flexShrink: 0,
                transition: 'opacity 200ms ease-in-out', pointerEvents: 'none',
              }}>
                <LottieRenderer
                  animationData={currentSource!.uri}
                  width={PET_W}
                  height={PET_H}
                />
              </div>
            )
          }

          return (
            <img
              src={resolveSvg(effectiveState)}
              // Decorative: the art is the pet's ornament and carries no information
              // of its own — the wrapping control supplies the accessible name, and
              // the pet's actual state reaches the user through the bubble. Naming
              // the pose here would announce it a second time on every transition.
              alt=""
              style={{
                width: PET_W, height: PET_H, opacity, flexShrink: 0,
                transition: 'opacity 200ms ease-in-out', pointerEvents: 'none',
              }}
              title={`${petName}: ${effectiveState}`}
              draggable={false}
            />
          )
        })()}
      </div>

      <style>{`
        @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes fadeOut { from { opacity: 1; transform: translateY(0); } to { opacity: 0; transform: translateY(-8px); } }
      `}</style>

      {ctxMenu && <PetContextMenu x={ctxMenu.x} y={ctxMenu.y} isHidden={false} onClose={() => setCtxMenu(null)} />}
    </>
  )
}
