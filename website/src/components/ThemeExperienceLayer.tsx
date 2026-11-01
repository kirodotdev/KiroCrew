/**
 * Level-2 (Experience) theme layer.
 *
 * Mounts the active installed theme's sandboxed overlay/topbar iframes, runs a
 * strict postMessage Message Router (only the `theme:resize|sound|visibility`
 * allowlist is honoured, and only from our own theme iframes), and drives an
 * opt-in audio engine. A pack has up to three parts, each on or off on its own
 * (utils/themeConsent.ts): sounds, overlays (with the topbar strip) and a
 * persona the agent adopts in its system prompt (backend, §6.5). A pack that
 * ships sounds or a persona asks on first activation, one checkbox per part;
 * "Keep colors only" turns every part off. It asks again about the persona
 * alone when the persona is wanted without a matching grant (turned on in
 * Settings, or changed); "Keep persona off" there turns only the persona off.
 * The palette applies either way.
 *
 * Security posture (§8.2): overlay/topbar iframes are `sandbox="allow-scripts"`
 * WITHOUT `allow-same-origin`, so they run in an opaque origin and cannot read
 * the dashboard's cookies/storage. Their HTML is served with a locked-down CSP
 * (see the backend asset routes). The router validates `event.source` against
 * our live iframe windows before honouring any message, and ignores every type
 * outside the allowlist. Overlays and audio additionally respect
 * prefers-reduced-motion and a user mute toggle.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import { Volume2, VolumeX } from 'lucide-react'
import {
  useTheme,
  type ThemeAudioManifest,
  type ThemeOverlayDecl,
  type ThemeOverlayPosition,
} from '../hooks/useTheme'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
// The choice and the persona grant live in ONE module so the grant format can't
// drift between this layer and the wire reader (api/client/chat.ts).
import {
  experiencePack,
  keepColorsOnly,
  getStoredConsent,
  readExperience,
  saveExperienceChoice,
  settleExperience,
  subscribeExperience,
  type ExperienceChoice,
  type ExperiencePart,
  type ExperienceState,
} from '../utils/themeConsent'
import ErrorNotice from './ErrorNotice'
import { Btn, Checkbox } from './ui'
// App-event → theme-audio bridge: real chat/notification events route to the
// active theme's manifest sounds (message-received / notification triggers).
import { MC_THEME_SOUND_EVENT, type ThemeSoundDetail } from '../hooks/themeSound'
import { MC_NOTIFICATION_EVENT } from '../hooks/notificationEvent'
import { useIsNarrowViewport } from '../hooks/useIsMobile'
import { useReducedMotion } from '../hooks/useReducedMotion'
import { OVERLAY_Z_MAX, useThemeDecorBehindSlot, useThemeDecorSlot } from '../lib/themeDecorLayer'
import { useAppSelector } from '../store'

import { i18nT } from '../i18n/t'
// postMessage types accepted from theme iframes — everything else is ignored.
const ALLOWED_MSG = new Set(['theme:resize', 'theme:sound', 'theme:visibility'])
const MUTE_KEY = 'mc-theme-muted'
const MAX_FRAME_PX = 4096
const MAX_CONCURRENT_SOUNDS = 4
// Topbar height px-equivalent ceiling — mirrors backend _THEME_TOPBAR_MAX_PX.
// A pointer-interactive topbar clamps its runtime theme:resize height to this
// (a thin strip that provably cannot become a viewport-covering interaction
// interceptor), closing the gap that the install-time clamp alone leaves open
// on the runtime resize path (see the Message Router).
const TOPBAR_MAX_PX = 200
// Overlay zIndex ceiling: the backend allows up to 9999, but overlays must sit
// strictly below the top bar (TOPBAR_Z / TOPBAR_FOCUS_Z in lib/themeDecorLayer.ts,
// which derives OVERLAY_Z_MAX from them) and, critically, below the mute button
// (z=50) and the consent modal (z=120) so those stay clickable. The clamp alone
// is not what enforces this — overlays also portal INTO the shell's stacking
// context (see the render below); rendered outside it, a z of 2 would already
// paint over the header (#7377).
// `activate` + `once` overlays play a one-shot animation on theme activation.
// The wire carries no per-overlay duration, so we auto-unmount after this
// window (agent-chosen default; the overlay may also self-hide via
// `theme:visibility` sooner, which the router already honours).
const ACTIVATE_ONCE_MS = 8000
// The input that resets an `idle-<N>s` overlay's countdown (see the idle effect).
const IDLE_INPUT_EVENTS = ['keydown', 'pointerdown', 'wheel', 'touchmove'] as const

const OVERLAY_POSITIONS: ReadonlySet<ThemeOverlayPosition> = new Set<ThemeOverlayPosition>([
  'top', 'bottom', 'left', 'right',
  'top-left', 'top-right', 'bottom-left', 'bottom-right',
  'center', 'fullscreen',
])
const OVERLAY_ANIMATIONS = new Set(['continuous', 'once', 'none'])
const OVERLAY_TRIGGER_RE = /^(continuous|activate|idle-[0-9]{1,3}s)$/

const assetBase = (slug: string) => `/api/theme/${encodeURIComponent(slug)}/assets`
const overlayUrl = (slug: string, id: string) =>
  `/api/theme/${encodeURIComponent(slug)}/overlay/${encodeURIComponent(id)}`
const topbarUrl = (slug: string, mode: string) =>
  `/api/theme/${encodeURIComponent(slug)}/topbar/${encodeURIComponent(mode)}`

/** Overlay ids are file stems — same safe-slug shape the backend enforces. */
const isSafeId = (s: string) => /^[a-z0-9-]{1,64}$/.test(s)
/** A theme:sound name must be a bare audio filename (no path, allowed ext). */
const isSafeAudioFile = (s: string) => /^[a-z0-9-]{1,64}\.(mp3|ogg|wav)$/.test(s)

/**
 * Normalize one raw `assets.overlays` entry into a `ThemeOverlayDecl`, tolerating
 * BOTH the new object shape and a stale descriptor's bare `string` id. Anything
 * with an unsafe/missing id is dropped (returns null); unknown enum values fall
 * back to the backend defaults, and zIndex is clamped into the safe band.
 */
function normalizeOverlay(raw: unknown): ThemeOverlayDecl | null {
  if (typeof raw === 'string') {
    if (!isSafeId(raw)) return null
    return {
      id: raw,
      position: 'fullscreen',
      zIndex: 40,
      pointerEvents: false,
      layer: 'above',
      animation: 'continuous',
      trigger: 'continuous',
    }
  }
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const id = o.id
  if (typeof id !== 'string' || !isSafeId(id)) return null
  const position = OVERLAY_POSITIONS.has(o.position as ThemeOverlayPosition)
    ? (o.position as ThemeOverlayPosition)
    : 'fullscreen'
  const zRaw = typeof o.zIndex === 'number' && Number.isFinite(o.zIndex) ? o.zIndex : 40
  const zIndex = Math.max(0, Math.min(OVERLAY_Z_MAX, Math.round(zRaw)))
  const animation = OVERLAY_ANIMATIONS.has(o.animation as string)
    ? (o.animation as ThemeOverlayDecl['animation'])
    : 'continuous'
  const trigger = typeof o.trigger === 'string' && OVERLAY_TRIGGER_RE.test(o.trigger)
    ? o.trigger
    : 'continuous'
  const layer = o.layer === 'behind' ? 'behind' : 'above'
  // Content covers a behind overlay everywhere, so it is click-through even if a stale descriptor says otherwise.
  return { id, position, zIndex, pointerEvents: layer === 'above' && o.pointerEvents === true, layer, animation, trigger }
}

/** Map a `position` enum value to a fixed-position CSS box. theme:resize may
 * later override width/height in px; the anchoring edges/corners are preserved. */
function overlayPlacement(position: ThemeOverlayPosition): CSSProperties {
  switch (position) {
    case 'top':
      return { top: 0, left: 0, right: 0, width: '100%', height: '30%' }
    case 'bottom':
      return { bottom: 0, left: 0, right: 0, width: '100%', height: '30%' }
    case 'left':
      return { top: 0, bottom: 0, left: 0, width: '30%', height: '100%' }
    case 'right':
      return { top: 0, bottom: 0, right: 0, width: '30%', height: '100%' }
    case 'top-left':
      return { top: 0, left: 0, width: '40%', height: '40%' }
    case 'top-right':
      return { top: 0, right: 0, width: '40%', height: '40%' }
    case 'bottom-left':
      return { bottom: 0, left: 0, width: '40%', height: '40%' }
    case 'bottom-right':
      return { bottom: 0, right: 0, width: '40%', height: '40%' }
    case 'center':
      return {
        top: '50%',
        left: '50%',
        width: '60%',
        height: '60%',
        transform: 'translate(-50%, -50%)',
      }
    case 'fullscreen':
    default:
      return { inset: 0, width: '100%', height: '100%' }
  }
}

type TriggerFamily = 'continuous' | 'activate' | 'idle'
function triggerFamily(trigger: string): TriggerFamily {
  if (trigger === 'activate') return 'activate'
  if (/^idle-[0-9]{1,3}s$/.test(trigger)) return 'idle'
  return 'continuous'
}
/** Seconds for an `idle-<N>s` trigger, else null. */
function parseIdleSeconds(trigger: string): number | null {
  const m = /^idle-([0-9]{1,3})s$/.exec(trigger)
  return m ? Number(m[1]) : null
}

/** Nothing on and nothing asked: a built-in theme, or a Level-1 render-cache seed. */
const EXPERIENCE_OFF: ExperienceState = { answered: false, sounds: false, overlays: false, persona: false, ask: [] }

function partLabel(part: ExperiencePart): string {
  if (part === 'sounds') return i18nT('components.themeExperienceLayer.sounds')
  if (part === 'overlays') return i18nT('components.themeExperienceLayer.overlays')
  return i18nT('components.themeExperienceLayer.persona')
}

export default function ThemeExperienceLayer() {
  const { theme: mode, colorTheme, customThemeDataMap } = useTheme()

  // A remote Crew is a separate SPA in a full-bleed iframe; switching Crews only
  // toggles `display` on the local dashboard's wrapper inside `App`. This layer is
  // a SIBLING of `<App />` (mounted in main.tsx): its topbar strip, mute button and
  // consent modal are `position: fixed` at the root, so App's own `display: none`
  // wrapper cannot hide them, and they composite above the remote pane stack at
  // z-1. Read the active remote-Crew id here so the render gate below can unmount
  // the whole layer, mirroring how App hides the local dashboard. (Decorative
  // overlays are the exception: they portal into the shell — see `decorSlot`.)
  const activeInstanceId = useAppSelector((s) => s.instances.activeId)
  // Decorative overlays render INSIDE the dashboard shell's stacking context so
  // the top bar outranks them (#7377); null while no shell is mounted
  // (onboarding, bootstrap), in which case they render inline at the root —
  // there is no chrome to sit under yet.
  const decorSlot = useThemeDecorSlot()
  const behindSlot = useThemeDecorBehindSlot()

  const active = colorTheme.startsWith('custom-')
    ? customThemeDataMap.get(colorTheme.slice('custom-'.length))
    : undefined
  const slug = active?.slug
  // The theme-list map rebuilds a fresh `active` object on every install /
  // create / delete (CUSTOM_THEMES_CHANGED_EVENT → loadCustomThemes), so
  // `active.assets` gets a NEW reference with identical content whenever ANY
  // theme changes. Cache a content-stable reference (keyed on a structural hash)
  // so the activation-scoped effects below don't churn on an unrelated list
  // rebuild — replaying the `activate` cue, stacking a duplicate ambient loop,
  // or resetting the once/idle overlay bookkeeping — when nothing actually
  // changed. A real content change (or theme switch) still flips the key and
  // re-activates.
  const rawAssets = active?.assets
  const assetsKey = rawAssets ? JSON.stringify(rawAssets) : ''
  const stableAssetsRef = useRef<{ key: string; value: typeof rawAssets }>({
    key: '',
    value: undefined,
  })
  if (stableAssetsRef.current.key !== assetsKey) {
    stableAssetsRef.current = { key: assetsKey, value: rawAssets }
  }
  const assets = stableAssetsRef.current.value
  const isL2 = (active?.level ?? 0) >= 2

  const overlayDecls = useMemo<ThemeOverlayDecl[]>(
    () =>
      isL2
        ? ((assets?.overlays as unknown[]) ?? [])
            .map(normalizeOverlay)
            .filter((d): d is ThemeOverlayDecl => d !== null)
            .slice(0, 5)
        : [],
    [isL2, assets],
  )
  const topbar = isL2 ? assets?.topbar : undefined
  const topbarHeight = topbar?.height || '28px'
  const topbarHideOnMobile = !!topbar?.hideOnMobile
  const hasTopbarForMode = !!(topbar && (topbar as Record<string, boolean>)[mode])
  const hasAudio = !!(isL2 && assets?.hasAudio)
  const hasPersona = !!(isL2 && assets?.hasPersona)
  const personaInfo = assets?.personaInfo
  const anyExperience = isL2 && (overlayDecls.length > 0 || hasTopbarForMode || hasAudio || hasPersona)

  // What the pack ships, and which parts are on (utils/themeConsent.ts). Read,
  // settled and revoked only against the server-confirmed Level-2 detail: the
  // render cache (themeRenderCache.ts) seeds a Level-1 projection of the active
  // pack before that detail arrives, with level capped to 1 and personaInfo
  // stripped, so a seed can neither validate a grant nor revoke one. When the
  // real detail lands, isL2 flips true and the persona token becomes the real
  // sha256 in the same commit.
  const pack = useMemo(() => experiencePack(isL2 ? assets : undefined), [isL2, assets])
  const [experience, setExperience] = useState<ExperienceState>(() =>
    slug && isL2 ? readExperience(slug, pack) : EXPERIENCE_OFF,
  )
  useEffect(() => {
    if (!slug || !isL2) {
      setExperience(EXPERIENCE_OFF)
      return
    }
    // Migrates an older build's answer and revokes a grant the choice does not
    // back (a changed persona, a persona turned off or no longer shipped), so a
    // stale grant is never sent before the dialog is answered.
    settleExperience(slug, pack)
    const reread = () => setExperience(readExperience(slug, pack))
    reread()
    // Settings and other tabs change the choice without a theme switch.
    return subscribeExperience(slug, reread)
  }, [slug, isL2, pack])

  // Set when Save could not store the persona grant. The saved choice then has
  // the persona off, which would close the dialog, so it stays open on the
  // persona alone until a Save stores the grant or the persona is kept off.
  const [failedSave, setFailedSave] = useState<{ slug: string; ask: ExperiencePart[]; answered: boolean } | null>(null)
  const failed = failedSave && failedSave.slug === slug ? failedSave : null
  const dialogAsk = failed ? failed.ask : experience.ask
  const dialogAnswered = failed ? failed.answered : experience.answered
  const showConsent = anyExperience && dialogAsk.length > 0
  // While the dialog is open nothing of the experience runs.
  const soundsOn = experience.sounds && !showConsent
  const overlaysOn = experience.overlays && !showConsent

  const reduced = useReducedMotion()
  const [muted, setMuted] = useState(() => safeGetItem(MUTE_KEY) === '1')
  // NOT useIsMobile: this layer mounts above the router on every route, embed included,
  // so the hook's `/embed/` always-false carve-out would un-hide a hideOnMobile topbar.
  const isNarrow = useIsNarrowViewport()
  // Overlay trigger bookkeeping: ids of idle-overlays currently visible, and ids
  // of `activate`+`once` overlays whose one-shot window has elapsed.
  const [idleOverlayIds, setIdleOverlayIds] = useState<ReadonlySet<string>>(new Set())
  const [activateOnceExpired, setActivateOnceExpired] = useState<ReadonlySet<string>>(new Set())
  const audioEnabled = soundsOn && hasAudio && !muted && !reduced

  // Which overlays are mounted right now, honouring reduced-motion + trigger.
  const mountedOverlays = useMemo<ThemeOverlayDecl[]>(() => {
    if (!overlaysOn || reduced) return []
    return overlayDecls.filter((d) => {
      const fam = triggerFamily(d.trigger)
      if (fam === 'activate') return !(d.animation === 'once' && activateOnceExpired.has(d.id))
      if (fam === 'idle') return idleOverlayIds.has(d.id)
      return true // continuous
    })
  }, [overlaysOn, reduced, overlayDecls, activateOnceExpired, idleOverlayIds])

  const showTopbar = overlaysOn && hasTopbarForMode && !(topbarHideOnMobile && isNarrow)

  // ── Audio engine (§3.3/§4.3) ──
  // The wire now carries a trigger NAME (`theme:sound {trigger}`) resolved
  // against `assets.audio.triggers`; each entry declares its own src/volume and
  // a maxDuration cap we enforce by stopping the element. `enqueueAudio` is the
  // shared core (prune → cap → create → volume/fadeIn → auto-stop → play); it is
  // stable (touches only refs) so callers gate on `audioEnabled` themselves.
  const soundsRef = useRef<HTMLAudioElement[]>([])
  // setTimeout (maxDuration) + setInterval (fadeIn ramp) ids awaiting cleanup.
  const soundTimersRef = useRef<Set<number>>(new Set())
  const stopAllSounds = useCallback(() => {
    for (const id of soundTimersRef.current) {
      clearTimeout(id)
      clearInterval(id)
    }
    soundTimersRef.current.clear()
    for (const a of soundsRef.current) {
      try {
        a.pause()
        a.src = ''
      } catch {
        /* best-effort teardown */
      }
    }
    soundsRef.current = []
  }, [])

  const enqueueAudio = useCallback(
    (
      url: string,
      opts: { volume?: number; loop?: boolean; fadeIn?: number; maxDurationMs?: number },
    ) => {
      // Prune only FINISHED one-shots; keep buffering/playing/looping tracked.
      // A freshly-created Audio is `paused===true` while it buffers, so pruning
      // on `!a.paused` would drop just-created sounds — defeating the
      // concurrency cap and leaking audio that stopAllSounds() can never reach.
      soundsRef.current = soundsRef.current.filter((a) => !a.ended)
      if (soundsRef.current.length >= MAX_CONCURRENT_SOUNDS) return
      try {
        const el = new Audio(url)
        const target =
          typeof opts.volume === 'number' && Number.isFinite(opts.volume)
            ? Math.max(0, Math.min(1, opts.volume))
            : 1
        el.loop = opts.loop === true
        const remove = () => {
          soundsRef.current = soundsRef.current.filter((a) => a !== el)
        }
        el.addEventListener('ended', remove)
        const fade = typeof opts.fadeIn === 'number' && opts.fadeIn > 0 ? opts.fadeIn : 0
        if (fade > 0) {
          // Ramp volume 0 → target over `fade` seconds (ambient bed fade-in).
          try {
            el.volume = 0
          } catch {
            /* mock element */
          }
          const steps = 20
          const stepMs = Math.max(20, Math.round((fade * 1000) / steps))
          let i = 0
          const iv = window.setInterval(() => {
            i += 1
            try {
              el.volume = Math.min(target, (target * i) / steps)
            } catch {
              /* mock element */
            }
            if (i >= steps) {
              clearInterval(iv)
              soundTimersRef.current.delete(iv)
            }
          }, stepMs)
          soundTimersRef.current.add(iv)
        } else {
          try {
            el.volume = target
          } catch {
            /* mock element */
          }
        }
        // Enforce the per-trigger maxDuration cap (one-shots only; loops run
        // until torn down). Stop the element and drop it when the cap elapses.
        if (opts.maxDurationMs && opts.maxDurationMs > 0 && !el.loop) {
          const to = window.setTimeout(() => {
            try {
              el.pause()
              el.src = ''
            } catch {
              /* mock element */
            }
            remove()
            soundTimersRef.current.delete(to)
          }, opts.maxDurationMs)
          soundTimersRef.current.add(to)
        }
        void el.play().catch(() => {
          /* autoplay blocked / decode error — non-fatal */
        })
        soundsRef.current.push(el)
      } catch {
        /* Audio unsupported (SSR/test) — ignore */
      }
    },
    [],
  )

  // Resolve a trigger name → manifest entry and play it (unknown trigger or no
  // manifest ⇒ ignored silently). Gated on the live audio-enabled state.
  const playTrigger = useCallback(
    (trigger: string) => {
      if (!audioEnabled || !slug) return
      const entry = assets?.audio?.triggers?.[trigger]
      if (!entry || typeof entry.src !== 'string') return
      enqueueAudio(`${assetBase(slug)}/${entry.src}`, {
        volume: entry.volume,
        loop: false,
        maxDurationMs: entry.maxDuration > 0 ? entry.maxDuration * 1000 : 0,
      })
    },
    [audioEnabled, slug, assets, enqueueAudio],
  )

  // Guards the once-per-activation audio emission below so it fires once per
  // (slug, audio-enabled) activation rather than per `assets` identity. Set to
  // the active slug after we emit the activate cue + ambient bed; cleared on a
  // real slug change AND whenever audio is disabled, so re-enabling (unmute /
  // reduced-motion off / consent) still restarts the theme's audio.
  const activatedForSlugRef = useRef<string | undefined>(undefined)

  // Tear sounds down when the active theme changes (a direct A→B switch keeps
  // audioEnabled true, so this slug-keyed effect is what stops A's loops), when
  // audio becomes disabled (mute / reduced-motion), and on unmount.
  useEffect(() => {
    stopAllSounds()
    activatedForSlugRef.current = undefined
  }, [slug, stopAllSounds])
  useEffect(() => {
    if (!audioEnabled) {
      stopAllSounds()
      activatedForSlugRef.current = undefined
    }
  }, [audioEnabled, stopAllSounds])
  useEffect(() => stopAllSounds, [stopAllSounds])
  // Switching to a remote Crew unmounts this layer (see the render gate below),
  // but React runs that teardown only after the commit — and themed audio lives in
  // refs, not in the tree. Stop it as soon as a remote Crew becomes active so its
  // ambient bed can't keep playing over another Crew's dashboard.
  useEffect(() => {
    if (activeInstanceId !== null) {
      stopAllSounds()
      activatedForSlugRef.current = undefined
    }
  }, [activeInstanceId, stopAllSounds])

  // Dashboard-side trigger emission. On activation of an enabled audio theme:
  // play the `activate` cue and start the ambient bed (fade-in). On switch-away:
  // play the LEAVING theme's `deactivate` cue — this effect is declared AFTER
  // the slug-change teardown above, so that teardown has already stopped the old
  // loops this cycle and our short deactivate cue survives (it self-stops at its
  // cap). message-sent/received/error/notification are intentionally NOT wired
  // into app internals (out of scope); they remain playable via `theme:sound`.
  const prevAudioRef = useRef<{ slug: string; manifest?: ThemeAudioManifest; canPlay: boolean } | null>(
    null,
  )
  useEffect(() => {
    const prev = prevAudioRef.current
    if (prev && prev.slug && prev.slug !== slug && prev.canPlay) {
      const d = prev.manifest?.triggers?.deactivate
      if (d && typeof d.src === 'string') {
        enqueueAudio(`${assetBase(prev.slug)}/${d.src}`, {
          volume: d.volume,
          loop: false,
          maxDurationMs: (d.maxDuration > 0 ? d.maxDuration : 2) * 1000,
        })
      }
    }
    if (audioEnabled && slug && activatedForSlugRef.current !== slug) {
      playTrigger('activate')
      const amb = assets?.audio?.ambient
      if (amb && typeof amb.src === 'string') {
        enqueueAudio(`${assetBase(slug)}/${amb.src}`, {
          volume: amb.volume,
          loop: amb.loop !== false,
          fadeIn: amb.fadeIn,
        })
      }
      // Mark this (slug, enabled) activation done so an unrelated theme-list
      // rebuild (fresh `assets` identity, same content) can't replay the cue or
      // stack a second ambient loop.
      activatedForSlugRef.current = slug
    }
    prevAudioRef.current = { slug: slug ?? '', manifest: assets?.audio, canPlay: audioEnabled }
  }, [slug, audioEnabled, assets, playTrigger, enqueueAudio])

  // App-event → theme-audio bridge. Real app events drive the manifest's
  // event-triggered sounds (the 7-name taxonomy's message-received / notification
  // slots): an agent reply arriving fires MC_THEME_SOUND_EVENT{message-received},
  // and the existing notification event (shared with useNotificationSound) maps to
  // the `notification` trigger. playTrigger self-gates on consent/mute/reduced-
  // motion + a matching manifest entry, so an event with no matching trigger (or a
  // muted theme) is a silent no-op. Only attach while Sounds is on.
  useEffect(() => {
    if (!soundsOn) return
    const onThemeSound = (e: Event) => {
      const trig = (e as CustomEvent<ThemeSoundDetail>).detail?.trigger
      if (typeof trig === 'string' && trig) playTrigger(trig)
    }
    const onNotification = () => playTrigger('notification')
    window.addEventListener(MC_THEME_SOUND_EVENT, onThemeSound)
    window.addEventListener(MC_NOTIFICATION_EVENT, onNotification)
    return () => {
      window.removeEventListener(MC_THEME_SOUND_EVENT, onThemeSound)
      window.removeEventListener(MC_NOTIFICATION_EVENT, onNotification)
    }
  }, [soundsOn, playTrigger])

  // `activate`+`once` overlays: mount on activation, then auto-unmount after the
  // one-shot window. Re-keys on overlayDecls (a theme switch), so a fresh
  // activation re-shows them. `continuous`/`none` activate overlays stay mounted.
  useEffect(() => {
    setActivateOnceExpired(new Set())
    if (!overlaysOn || reduced) return
    const onceIds = overlayDecls
      .filter((d) => triggerFamily(d.trigger) === 'activate' && d.animation === 'once')
      .map((d) => d.id)
    if (onceIds.length === 0) return
    const timers = onceIds.map((id) =>
      window.setTimeout(() => {
        setActivateOnceExpired((prev) => new Set(prev).add(id))
      }, ACTIVATE_ONCE_MS),
    )
    return () => timers.forEach((t) => clearTimeout(t))
  }, [overlaysOn, reduced, overlayDecls])

  // `idle-<N>s` overlays: mount after N seconds with no input; any input hides
  // them and restarts the countdown. Input is a key press, a pointer press, or a
  // wheel or touch scroll, inside a scroll container too, so an overlay never
  // lands on someone reading the transcript with the trackpad. The listeners sit
  // in the CAPTURE phase because the built-in terminal (xterm) stops propagation
  // of the keys and wheel events it consumes, so a bubble-phase listener on the
  // window would not see typing there. Wheel and touchmove fire many times a second,
  // so an event only records when it happened; a countdown that comes due checks
  // that time and waits out the remainder, and state changes only when an
  // overlay is actually showing.
  useEffect(() => {
    if (!overlaysOn || reduced) {
      setIdleOverlayIds(new Set())
      return
    }
    const idleDecls = overlayDecls
      .map((d) => ({ id: d.id, secs: parseIdleSeconds(d.trigger) }))
      .filter((d): d is { id: string; secs: number } => d.secs !== null)
    if (idleDecls.length === 0) {
      setIdleOverlayIds(new Set())
      return
    }
    const timers = new Map<string, number>()
    let lastInputAt = Date.now()
    let showing = false
    const countDown = (d: { id: string; secs: number }, delayMs: number) => {
      timers.set(d.id, window.setTimeout(() => {
        const remainingMs = d.secs * 1000 - (Date.now() - lastInputAt)
        if (remainingMs > 0) {
          countDown(d, remainingMs)
          return
        }
        showing = true
        setIdleOverlayIds((prev) => new Set(prev).add(d.id))
      }, delayMs))
    }
    const armAll = () => idleDecls.forEach((d) => countDown(d, d.secs * 1000))
    const onInput = () => {
      lastInputAt = Date.now()
      if (!showing) return
      showing = false
      timers.forEach((t) => clearTimeout(t))
      timers.clear()
      setIdleOverlayIds(new Set())
      armAll()
    }
    const opts: AddEventListenerOptions = { capture: true, passive: true }
    for (const type of IDLE_INPUT_EVENTS) window.addEventListener(type, onInput, opts)
    setIdleOverlayIds(new Set())
    armAll()
    return () => {
      for (const type of IDLE_INPUT_EVENTS) window.removeEventListener(type, onInput, opts)
      timers.forEach((t) => clearTimeout(t))
    }
  }, [overlaysOn, reduced, overlayDecls])

  // ── theme:state downlink (§4.3) ── Dashboard → each live theme iframe.
  // Sandboxed frames are opaque-origin, so '*' is required; payload carries no
  // secrets. Posted (a) on each frame's `load` (see onLoad below) and (b) here
  // whenever mode / muted / reduced-motion change — or the mounted frame set does.
  const postThemeState = useCallback(
    (win: Window | null | undefined) => {
      if (!win) return
      try {
        // Sandboxed theme iframes (sandbox="allow-scripts" WITHOUT
        // allow-same-origin) have an opaque origin, so no concrete
        // targetOrigin can ever match; the payload carries only
        // {mode, muted, reducedMotion} — no secrets. '*' is required here.
        // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
        win.postMessage(
          { type: 'theme:state', mode, muted, reducedMotion: reduced },
          '*',
        )
      } catch {
        /* detached / opaque frame — best-effort */
      }
    },
    [mode, muted, reduced],
  )
  useEffect(() => {
    if (!overlaysOn) return
    const frames = document.querySelectorAll<HTMLIFrameElement>('iframe[data-theme-frame="1"]')
    frames.forEach((f) => postThemeState(f.contentWindow))
  }, [overlaysOn, postThemeState, showTopbar, mountedOverlays])

  // ── Message Router (only our theme iframes, only the allowlisted types) ──
  // Every message comes from an overlay or topbar frame, which exist only while
  // Overlays is on; theme:sound additionally needs Sounds (audioEnabled).
  useEffect(() => {
    if (!overlaysOn) return
    const handler = (e: MessageEvent) => {
      const frames = Array.from(
        document.querySelectorAll<HTMLIFrameElement>('iframe[data-theme-frame="1"]'),
      )
      const frame = frames.find((f) => f.contentWindow === e.source)
      if (!frame) return // not one of our sandboxed theme iframes
      const data = e.data as { type?: unknown; width?: unknown; height?: unknown; visible?: unknown; trigger?: unknown; name?: unknown; loop?: unknown }
      const type = data?.type
      if (typeof type !== 'string' || !ALLOWED_MSG.has(type)) return
      if (type === 'theme:resize') {
        const w = Number(data.width)
        const h = Number(data.height)
        // Clamp against the frame's DECLARED bounds, not one global constant, so
        // a runtime resize cannot escape the install-time containment ceiling.
        // A declared height ceiling (topbar `data-theme-maxh`)
        // clamps height regardless of pointer state — a full-width strip stays a
        // strip even though it is click-through. Pointer-interactive overlays
        // additionally may not grow past their placement box (can't become a
        // viewport-covering click/redress surface); pointer-inert frames without
        // a declared ceiling keep the large cap (they can't intercept clicks).
        const interactive = frame.dataset.themePointer === '1'
        const declMaxH = Number(frame.dataset.themeMaxh)
        const hasDeclMaxH = Number.isFinite(declMaxH) && declMaxH > 0
        let maxW = MAX_FRAME_PX
        let maxH = hasDeclMaxH ? declMaxH : MAX_FRAME_PX
        if (interactive) {
          const rect = frame.getBoundingClientRect()
          maxW = Math.max(1, Math.ceil(rect.width))
          if (!hasDeclMaxH) maxH = Math.max(1, Math.ceil(rect.height))
        }
        if (Number.isFinite(w) && w > 0) frame.style.width = `${Math.round(Math.min(w, maxW))}px`
        if (Number.isFinite(h) && h > 0) frame.style.height = `${Math.round(Math.min(h, maxH))}px`
      } else if (type === 'theme:visibility') {
        frame.style.display = data.visible === false ? 'none' : ''
      } else if (type === 'theme:sound') {
        // Spec §3.3/§4.3: payload is a trigger NAME resolved via the manifest.
        // BACKWARD-COMPAT: if there's no manifest match but the payload carries a
        // legacy bare `name` filename (the shipped sample uses it), play that.
        if (!audioEnabled || !slug) return
        const trig = data.trigger
        if (typeof trig === 'string' && assets?.audio?.triggers?.[trig]) {
          playTrigger(trig)
        } else if (typeof data.name === 'string' && isSafeAudioFile(data.name)) {
          enqueueAudio(`${assetBase(slug)}/audio/${data.name}`, {
            volume: 1,
            loop: data.loop === true,
          })
        }
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [overlaysOn, audioEnabled, slug, assets, playTrigger, enqueueAudio])

  // Dialog answers, one per asked part; a part not yet touched counts as checked.
  const [picks, setPicks] = useState<ExperienceChoice>({})
  useEffect(() => {
    if (showConsent) setPicks({})
  }, [showConsent])

  // Save: the asked parts take their checkbox, the rest keep the stored choice.
  // The persona grant is bound to the fingerprint of the text shown in the dialog.
  const saveChoice = useCallback(() => {
    if (!slug) return
    const next: ExperienceChoice = dialogAnswered
      ? { sounds: experience.sounds, overlays: experience.overlays, persona: experience.persona }
      : {}
    for (const part of dialogAsk) next[part] = picks[part] !== false
    saveExperienceChoice(slug, next, pack.personaToken)
    // A choice that only this tab keeps still applies, so the dialog closes. A
    // persona grant that could not be stored leaves the persona off; the dialog
    // stays open on the persona alone, since chat sends only a stored grant.
    const grantMissing = next.persona === true && pack.personaToken !== null && getStoredConsent(slug) !== pack.personaToken
    setFailedSave(grantMissing ? { slug, ask: ['persona'], answered: true } : null)
  }, [slug, experience, dialogAsk, dialogAnswered, picks, pack.personaToken])

  // Decline ("Keep colors only" on a first answer, "Keep persona off" when only
  // the persona is asked about; Escape does the same): the palette stays
  // (useTheme applies it whatever this layer decides) and any grant is revoked.
  // On a first answer every part is off; on the persona-only ask the persona is
  // off and the stored sounds and overlays stay as they are.
  const keepColors = useCallback(() => {
    if (!slug) return
    setFailedSave(null)
    // A failed store needs no notice: the off choice is kept for this tab and the dialog closes.
    if (dialogAnswered) {
      saveExperienceChoice(slug, { sounds: experience.sounds, overlays: experience.overlays, persona: false }, null)
    } else {
      keepColorsOnly(slug)
    }
  }, [slug, dialogAnswered, experience.sounds, experience.overlays])

  const toggleMute = useCallback(() => {
    setMuted((m) => {
      const next = !m
      safeSetItem(MUTE_KEY, next ? '1' : '0')
      return next
    })
  }, [])

  const saveBtnRef = useRef<HTMLButtonElement>(null)
  // Consent-modal a11y: focus the primary action on open and let Escape press
  // the decline button. Backdrop already blocks pointer interaction.
  useEffect(() => {
    if (!showConsent) return
    saveBtnRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') keepColors()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [showConsent, keepColors])

  // A remote Crew owns the viewport: unmount every overlay/topbar iframe, the mute
  // button and the consent modal. `display: none` on App's local-dashboard wrapper
  // cannot reach a fixed-position sibling, so unmounting is the only real teardown.
  if (activeInstanceId !== null) return null
  if (!anyExperience || !slug) return null

  if (showConsent) {
    return (
      <div
        className="fixed inset-0 z-[120] flex items-center justify-center bg-black/50 p-4"
        role="dialog"
        aria-modal="true"
        aria-labelledby="theme-consent-title"
      >
        <div className="w-full max-w-md rounded-xl border border-border bg-card p-5 text-text shadow-2xl">
          <h2 id="theme-consent-title" className="text-base font-semibold text-text-strong">
            {i18nT('components.themeExperienceLayer.enable_named_experience', { name: active?.name })}
          </h2>
          <div className="mt-3 space-y-2">
            {dialogAsk.map((part) => (
              <div key={part}>
                <label className="flex items-center gap-2 text-sm text-text cursor-pointer">
                  <Checkbox
                    checked={picks[part] !== false}
                    onChange={(e) => {
                      const on = e.currentTarget.checked
                      setPicks((p) => ({ ...p, [part]: on }))
                    }}
                  />
                  {partLabel(part)}
                </label>
                {part === 'persona' && personaInfo?.text && (
                  <div className="mt-1 pl-6">
                    <p className="text-[12px] font-medium text-muted">
                      {i18nT('components.themeExperienceLayer.persona_that_will_be_added_to_the_assistant_s_sy')}
                    </p>
                    <pre
                      className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-bg-elevated p-2 text-[12px] text-text"
                      aria-label={i18nT('components.themeExperienceLayer.persona_text')}
                      data-testid="consent-persona-text"
                    >
                      {personaInfo.text}
                    </pre>
                  </div>
                )}
              </div>
            ))}
          </div>
          {/* No hand-off: the checkbox picks in this dialog are unsaved until
              Save succeeds, and a navigation would discard them. */}
          <ErrorNotice
            className="mt-3"
            message={failed ? i18nT('components.themeExperienceLayer.save_failed_storage') : null}
          />
          <div className="mt-5 flex justify-end gap-2">
            <Btn type="button" onClick={keepColors}>
              {dialogAnswered
                ? i18nT('components.themeExperienceLayer.keep_persona_off')
                : i18nT('components.themeExperienceLayer.keep_colors_only')}
            </Btn>
            <Btn type="button" primary ref={saveBtnRef} onClick={saveChoice}>
              {i18nT('components.themeExperienceLayer.save')}
            </Btn>
          </div>
        </div>
      </div>
    )
  }

  // Decorative overlays — manifest-driven placement/behaviour. mountedOverlays
  // is already [] under reduced-motion, so motion overlays stay suppressed.
  // Built here because they render in one of two places per layer (see below).
  const overlayFrame = (decl: ThemeOverlayDecl) => (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <iframe
      key={decl.id}
      data-theme-frame="1"
      data-theme-pointer={decl.pointerEvents ? '1' : '0'}
      data-theme-layer={decl.layer}
      title={i18nT('components.themeExperienceLayer.theme_overlay', { id: decl.id })}
      src={overlayUrl(slug, decl.id)}
      sandbox="allow-scripts"
      onLoad={(e) => postThemeState((e.currentTarget as HTMLIFrameElement).contentWindow)}
      style={{
        position: 'fixed',
        border: 'none',
        background: 'transparent',
        // See topbar note: opt out of the parent's dark color-scheme so a
        // full-viewport transparent overlay (e.g. Bikini's `bubbles`) does
        // not composite an opaque backdrop that hides the entire dashboard.
        colorScheme: 'normal',
        pointerEvents: decl.pointerEvents ? 'auto' : 'none',
        zIndex: decl.zIndex,
        ...overlayPlacement(decl.position),
      }}
    />
  )
  // With Sounds and Overlays both off nothing mounts, the mute button included.
  if (!overlaysOn && !soundsOn) return null

  const overlays = <>{mountedOverlays.filter((d) => d.layer !== 'behind').map(overlayFrame)}</>
  const behindOverlays = <>{mountedOverlays.filter((d) => d.layer === 'behind').map(overlayFrame)}</>

  return (
    <>
      {/* Themed topbar strip (static chrome — shown even under reduced-motion). */}
      {showTopbar && (
        // theme:state downlink is posted on the frame's load (see onLoad); the
        // effect fires pre-load so can't be relied on for the first post. iframe
        // is non-interactive, so the a11y interaction rule is a false positive.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <iframe
          data-theme-frame="1"
          data-theme-pointer="0"
          data-theme-maxh={String(TOPBAR_MAX_PX)}
          title={`theme topbar (${mode})`}
          src={topbarUrl(slug, mode)}
          sandbox="allow-scripts"
          onLoad={(e) => postThemeState((e.currentTarget as HTMLIFrameElement).contentWindow)}
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            width: '100%',
            height: topbarHeight,
            border: 'none',
            background: 'transparent',
            // Opt the sandboxed iframe out of the parent dashboard's dark
            // `color-scheme`, which otherwise makes the browser paint an OPAQUE
            // backdrop behind a `background:transparent` iframe — a full-viewport
            // decorative overlay would then composite opaque and hide the whole
            // app (the Bikini blank-screen bug). `normal` lets the transparent
            // background actually show the dashboard through.
            colorScheme: 'normal',
            // The topbar is DECORATIVE branding (sprite-walker) laid over the top
            // strip of the real dashboard — it must be click-through, or it eats
            // clicks meant for the controls beneath it. It is still height-clamped
            // (data-theme-maxh) so it can't cover the viewport. Unlike the overlays
            // below it is MEANT to paint over the header, so it stays at the root
            // (z-45 vs the shell's z-1) rather than in the decor slot.
            pointerEvents: 'none',
            zIndex: 45,
          }}
        />
      )}

      {/* Decorative overlays portal into the shell's decor slot so the header
          outranks them (#7377); inline at the root only while no shell exists. */}
      {decorSlot ? createPortal(overlays, decorSlot) : overlays}
      {/* Without a shell (popout, embed) nothing sits on top to cover them, so they are skipped. */}
      {behindSlot && createPortal(behindOverlays, behindSlot)}

      {/* Mute toggle — only while Sounds is on for a pack that ships audio. */}
      {soundsOn && hasAudio && (
        <button
          type="button"
          onClick={toggleMute}
          title={muted ? i18nT('components.themeExperienceLayer.unmute_theme_sounds') : i18nT('components.themeExperienceLayer.mute_theme_sounds')}
          aria-label={muted ? i18nT('components.themeExperienceLayer.unmute_theme_sounds') : i18nT('components.themeExperienceLayer.mute_theme_sounds')}
          className="fixed bottom-safe-offset-4 right-safe-offset-4 z-[50] flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-text shadow-lg hover:bg-bg-hover"
        >
          {muted ? <VolumeX size={16} /> : <Volume2 size={16} />}
        </button>
      )}
    </>
  )
}
