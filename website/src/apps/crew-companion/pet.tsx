/**
 * pet.tsx — the companion drawn into its transparent full-screen overlay.
 *
 * The window is click-through everywhere except the companion itself: the whole
 * display is covered, so a window that swallowed clicks would make the desktop
 * unusable. `pointer-events: none` on the body plus `auto` on the companion is the
 * DOM half of that; the Electron half is `setIgnoreMouseEvents(true, { forward:
 * true })` with a hit-test that re-enables input only over the sprite.
 *
 * Two jobs, both polled because the gateway offers no server-push channel to an
 * app's own windows:
 *
 *   * **presence** — tell the backend someone is here, or break nudges are
 *     suppressed. Must be more often than the 90s TTL in store.py.
 *   * **pending** — collect what fired and draw it as a bubble, obeying the
 *     per-kind rules the desktop app used (a break nudge clears itself, a reminder
 *     the user set waits for them).
 */
import { StrictMode, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
// This window is its OWN page entry, so it does not inherit the dashboard's
// bootstrap: i18n has to be initialised here or every translated string renders as
// its bare key. Break nudges are the ones that matter — the backend sends a key and
// this side supplies the sentence — and without init they resolved to nothing, so
// the companion stayed silent while reminders (which carry their own text) worked.
import { adoptDashboardTheme, watchThemeChanges } from './dashboardTheme'
import { initI18n } from '../../i18n'
import { i18nT } from '../../i18n/t'
import { PENDING_PATH, PRESENCE_PATH } from './constants'
import { nudgeTextFor } from './nudgeKeys'
import { PetAvatar, type PetState } from './PetAvatar'
import { usePlayfulMotion } from './usePlayfulMotion'
import { petBridge } from './petBridge'
import { watchSessions } from './sessionWatch'
import { randomCelebrateProp, type GhostAccessory } from './ghostAccessories'

import { PetContextMenu } from './PetContextMenu'
import { dragGrip } from './dragGrip'
import { useDrag } from './useDrag'
import { useMouseForward } from './useMouseForward'
import { Bubble } from './Bubble'
import { isSticky, type NotifKind } from './notificationPolicy'
import {
  pickBubblePlacement,
  resolveBubbleRect,
  BUBBLE_LAYOUT_DEFAULTS,
  type AnchorRect,
  type Rect,
} from './bubbleLayout'
import { nextBubble, STICKY_HOLD_MS, type PendingBubble } from './bubbleSlot'
import { useMood } from './useMood'
import { useEdgeHide } from './useEdgeHide'
import { useWalking } from './useWalking'
import { useIdleFidget } from './useIdleFidget'
import { useRandomClips, type RandomBehaviors } from './useRandomClips'
import { activeAnimFor, CELEBRATE_MS, CELEBRATE_PROP_HOLD_MS, type PetAnim } from './petAnim'
import { ALL_MOODS } from './appearanceTypes'

/** Well inside the backend's 90s presence TTL, so one dropped request is harmless. */
/**
 * The companion's rendered size, from PET_W/PET_H in the desktop app's
 * shared/constants.ts. Not a free choice: the drag grip centres on it and the
 * bubble is positioned from it, so a different number here desynchronises both.
 */
const PET_PX = 128

/** Tilt when docked at a screen edge, from DOCK_ROTATE in PetWidget. */
/**
 * How far the pointer may travel and still count as a tap rather than a drag.
 * From PetWidget's `moved > 6` check.
 */
const CLICK_SLOP = 6

const DOCK_ROTATE = 25


const PRESENCE_MS = 30_000
/** Reminders are minute-grained; two seconds is imperceptible and cheap on loopback. */
const PENDING_MS = 2_000

/**
 * A window command (open panel / gallery) older than this is ignored. The page
 * records these in the backend and this overlay carries them out on its poll; on
 * a relaunch the overlay drains the whole backlog from cursor 0, and popping a
 * window open from a click made minutes ago is intrusive in a way a stale nudge
 * is not — so a command only acts while it is fresh.
 */
const COMMAND_FRESH_MS = 15_000

/**
 * The preload bridge. Optional because this same page is openable in an ordinary
 * browser for development, where there is no main process to talk to — the pet
 * still renders and polls, it just cannot toggle window input.
 */
declare global {
  interface Window {
    crewCompanion?: {
      /** Granted only while the panel is open; see pet-preload.js. */
      setFocusable(focusable: boolean): void
      /** Open the panel window beside the companion, in screen coordinates. */
      panelOpen(petRect: { x: number; y: number; width: number; height: number }): void
      panelClose(): void
      /** Suppress the panel's close-on-blur while this is true. */
      panelHold(hold: boolean): void
      /** Fires when the panel window closes on its own — blur, Escape or its ✕. */
      onPanelClosed(cb: () => void): () => void
      galleryOpen(): void
      /** Fires when the avatar gallery window opens / closes, so the companion can
       *  hold still while the user is browsing it. */
      onGalleryOpened?(cb: () => void): () => void
      onGalleryClosed?(cb: () => void): () => void
    }
  }
}

interface Fire {
  seq: number
  kind: string
  text: string
  key: string
  at: string
}

interface Bubble {
  seq: number
  kind: NotifKind
  text: string
}

/**
 * Where the pending-fire cursor is kept across reloads.
 *
 * localStorage rather than the backend: the cursor describes what THIS overlay has
 * drawn, which is a renderer concern. Putting it in the store would make the read
 * destructive again and break the "two displays each get the bubble" property the
 * cursor design exists to protect.
 */
const CURSOR_KEY = 'cc:pendingCursor'

/** The persisted cursor, or 0 the first time this overlay ever runs. */
function readStoredCursor(): number {
  try {
    const n = Number(window.localStorage.getItem(CURSOR_KEY))
    // A corrupt or absent value must not silently mute every future reminder, so
    // anything unusable falls back to 0 (replay once) rather than to a large number.
    return Number.isFinite(n) && n > 0 ? n : 0
  } catch {
    return 0
  }
}

/** Remember it, so a restart resumes instead of replaying the history. */
function writeStoredCursor(n: number): void {
  try {
    window.localStorage.setItem(CURSOR_KEY, String(n))
  } catch {
    /* private mode or a full quota — replaying is bad but not fatal */
  }
}

/** Same-origin: the window is loaded from the gateway, so its cookie is present. */
async function post(path: string): Promise<void> {
  try {
    await fetch(path, { method: 'POST', credentials: 'same-origin' })
  } catch {
    /* the companion must never crash on a failed poll */
  }
}

/**
 * The main process refuses mouse input for this window by default, because the
 * window covers the entire display. CSS `pointer-events` cannot override that —
 * it governs which ELEMENT receives an event the window has already accepted.
 *
 * Rather than toggle input on pointer enter/leave over each element — which needed
 * an IPC round-trip and let a fast click fall through — the renderer reports the
 * companion's, bubble's and menu's rects and the main process polls the cursor and
 * toggles ignore-mouse itself. `useMouseForward` sends the companion and bubble
 * rects; the context-menu component sends its own via `petBridge.setMenuHitbox`.
 */

function Companion() {
  const [bubble, setBubble] = useState<Bubble | null>(null)
  /**
   * The single notification slot, driven by `bubbleSlot`. It is a ref, not state,
   * because the poll below reads and rewrites it synchronously; the visible bubble
   * is the separate `bubble` state.
   */
  const slotRef = useRef<PendingBubble | null>(null)
  /**
   * Where the bubble is drawn, plus the arrow's x inside it. Computed from the REAL
   * placement algorithm once the bubble is measured, so it sits directly above the
   * companion instead of the old hand-rolled offset that pushed it up-and-left.
   */
  const [placement, setPlacement] = useState<{ rect: Rect; arrowX: number } | null>(null)
  const bubbleHostRef = useRef<HTMLDivElement>(null)
  /**
   * The panel opens on a click of the companion, per the v1.0 spec: it renders
   * INSIDE this full-display overlay as DOM, beside the companion, rather than in a
   * second window. Two OS windows cannot be kept reliably in step, and the panel has
   * to sit next to a companion that moves.
   */
  /** Where the press began, for the tap-vs-drag test in onClick. */
  const clickDownPt = useRef<{ x: number; y: number } | null>(null)
  const [panelOpen, setPanelOpen] = useState(false)
  // Peek/dock state the drag hook drives when the companion is left at a screen edge.
  // Centralised in useEdgeHide so `setIsPeeking` updates `isPeekingRef` SYNCHRONOUSLY
  // — useDrag reads that ref inside its mouse handlers, so a render-lagged ref (the
  // old inline `isPeekingRef.current = isPeeking`) left the dock state inconsistent.
  const { hideEdge, isPeeking, setIsPeeking, setHideEdge, isPeekingRef } = useEdgeHide()

  /**
   * Drag, from the desktop app's own hook — its maths, thresholds and
   * click-vs-drag disambiguation unchanged. Movement is pure DOM within this
   * overlay, which is what keeps it smooth.
   */
  /**
   * Mood state, ported from the desktop app. The autonomous wander flickers it
   * (curious/happy by day, sleepy at night); `clearPersistentMood` is handed to the
   * drag hook so grabbing the companion wakes it. Declared before `useDrag` because
   * that hook takes `clearPersistentMood` as an option.
   */
  const { mood, setMood, clearPersistentMood } = useMood()

  const { pos, setPos, onMouseDown, dragging, posReady, isDragging } = useDrag(
    {
      x: Math.max(0, window.innerWidth - PET_PX - 28),
      y: Math.max(0, window.innerHeight - PET_PX - 96),
    },
    {
    clearPersistentMood,
    displayState: 'idle',
    setDisplayState: () => {},
    isPeekingRef,
    setIsPeeking,
    setHideEdge,
    // The built-in ghost docks at an edge; a custom pack's art has no defined
    // silhouette to crop, which is why the desktop app gates this.
    allowPeek: true,
    getGrip: () => dragGrip({}),
    },
  )

  /**
   * Playful idle motion — the ported `usePlayfulMotion` hook. ONE rAF loop mutates
   * the art wrapper's transform directly (no per-frame React state): a gentle idle
   * bob, a few-px lean toward the cursor, and a downward nod on a poke. Gated so it
   * holds still while the companion is dragged or docked at a screen edge.
   */
  const artRef = useRef<HTMLDivElement>(null)
  const playActiveRef = useRef(false)
  /**
   * Only the built-in ghost gets the bob/lean/nod (and the cursor-tracking eyes) —
   * a custom pack bakes its own motion and eyes, so it stays still, exactly as the
   * desktop app gates it.
   */
  const [isDefaultPack, setIsDefaultPack] = useState(true)
  /**
   * The random behaviours the ACTIVE custom pack ships, for `useRandomClips`. Empty
   * for the built-in ghost, which fidgets through `useIdleFidget` instead. `extras` is
   * left empty: this build's `PetAvatar` renders by state slot, not by arbitrary
   * author-named clips, so there is nothing to play them through yet.
   */
  const [customBehaviors, setCustomBehaviors] = useState<RandomBehaviors>({
    walking: false, moods: [], extras: [],
  })
  const motionEnabledRef = useRef(true)
  motionEnabledRef.current = isDefaultPack
  useEffect(() => {
    const release = () => window.crewCompanion?.panelHold?.(false)
    // Window-level: a drag usually ends with the pointer well away from the companion.
    window.addEventListener('mouseup', release)
    return () => window.removeEventListener('mouseup', release)
  }, [])

  const facingRightRef = useRef(false)
  const { poke } = usePlayfulMotion(artRef, playActiveRef, motionEnabledRef, facingRightRef)

  /**
   * The avatar gallery is its own window with no reliable close signal to this
   * overlay, so the main process broadcasts open/close and we mirror it here — the
   * companion must not wander off while the user is picking an avatar.
   */
  const galleryOpenRef = useRef(false)
  useEffect(() => {
    const offOpen = window.crewCompanion?.onGalleryOpened?.(() => { galleryOpenRef.current = true })
    const offClose = window.crewCompanion?.onGalleryClosed?.(() => { galleryOpenRef.current = false })
    return () => { offOpen?.(); offClose?.() }
  }, [])

  // Track which appearance pack is active so custom packs opt out of the built-in
  // motion, and read what random behaviours a custom pack provides.
  useEffect(() => {
    let alive = true
    const read = () => {
      void petBridge.getCrewCompanionConfig?.().then(async (c) => {
        if (!alive) return
        // Session alerts ride along on the config read this effect already does, and
        // re-reads on `config:updated` with it, so toggling the switch in the panel
        // reaches the watcher without another poll.
        sessionAlertsRef.current = c?.sessionNotificationsEnabled !== false
        // The gallery writes this under `kiro.accessory`; anything unknown means
        // "no prop" rather than a guess at what the user meant.
        const worn = (c as { kiro?: { accessory?: unknown } })?.kiro?.accessory
        setSavedProp(typeof worn === 'string' ? (worn as GhostAccessory) : 'none')
        const packId = c?.activeAppearance || 'kiro-ghost'
        const isDefault = packId === 'kiro-ghost'
        setIsDefaultPack(isDefault)
        if (isDefault) {
          setCustomBehaviors({ walking: false, moods: [], extras: [] })
          return
        }
        const detail = await petBridge.galleryGetPackDetail?.(packId).catch(() => null)
        if (!alive) return
        const anims = detail?.animations ?? {}
        const has = (k: string) => Object.prototype.hasOwnProperty.call(anims, k)
        /*
         * `extras` was hardcoded empty with a comment admitting there was "nothing
         * to play them through yet" — so every PetDex pack's wave / waiting / run
         * clips were imported, stored, listed, and never once shown. PetAvatar now
         * has a clip channel (`clipName`), so the names the pack actually ships can
         * finally enter the idle rotation. `randomNames` is the backend's
         * authoritative list; the flat-map filter is only a guard against a stale
         * name pointing at art that failed to read.
         */
        const randomNames: string[] = Array.isArray(detail?.randomNames)
          ? (detail.randomNames as string[]).filter((n) => has(n))
          : []
        setCustomBehaviors({
          walking: has('walking'),
          moods: (ALL_MOODS as readonly string[]).filter((m) => has(m)),
          extras: randomNames,
        })
      }).catch(() => {})
    }
    read()
    const off = petBridge.onGalleryActiveChanged?.(read)
    return () => { alive = false; off?.() }
  }, [])

  /** Right-click menu position, or null when closed. */
  const [menuAt, setMenuAt] = useState<{ x: number; y: number } | null>(null)

  /**
   * What the companion is reacting to — chooses both the pack's art slot and the
   * motion. In the desktop app a state manager in the main process broadcast this;
   * here the same information already arrives on the fire queue the pet polls, so the
   * kind of what just fired sets the state and it settles back to idle afterwards.
   */
  const [petState, setPetState] = useState<PetState>('idle')
  const stateTimer = useRef<number | null>(null)

  /**
   * A per-reaction counter, bumped every time a fresh reaction is triggered.
   *
   * Handed to PetAvatar, which folds it into the animated span's React key. Without
   * it the span is keyed only on the motion NAME, so a reaction that repeats the same
   * motion — a second completion is `celebrate` again, and a `happy` mood already
   * resolves to `celebrate` — reuses the same DOM node and never re-fires the CSS
   * keyframes, so the hop is silently skipped. Bumping this forces the remount that
   * replays the keyframes on every finish (and every error shake).
   */
  const [reactionEpoch, setReactionEpoch] = useState(0)
  const bumpReaction = useCallback(() => setReactionEpoch((n) => n + 1), [])

  /** Show a reaction, then return to idle. */
  const react = useCallback((next: PetState, holdMs: number) => {
    if (stateTimer.current !== null) window.clearTimeout(stateTimer.current)
    // A fresh reaction always replays its keyframes, even if it repeats the last one.
    setReactionEpoch((n) => n + 1)
    setPetState(next)
    stateTimer.current = window.setTimeout(() => setPetState('idle'), holdMs)
  }, [])

  /**
   * The last fire sequence this overlay has already shown, PERSISTED.
   *
   * Reading `/pending` is deliberately non-destructive on the backend, so that a
   * lost response or a second overlay on another display cannot make a reminder
   * vanish. The cost is that the queue outlives this page: starting from 0 on every
   * load re-delivered the whole history, so every restart of the desktop shell
   * replayed a reminder that had already fired and already been seen — a bubble that
   * came back no matter how many times it was closed.
   *
   * The desktop app this was ported from never hit this: its queue lived in the
   * Electron main process and died with the app. Here the queue is in the gateway
   * store and survives, so the cursor has to survive with it.
   *
   * Persisting rather than "start at the current cursor and ignore the backlog":
   * a reminder that fired while the companion was off must still arrive late — the
   * backend says so out loud ("a time the user chose must still arrive, late, on
   * their return"). Skipping the backlog would silently drop exactly that.
   *
   * KNOWN LIMIT: one key for the whole origin, so two overlays on two displays share
   * a cursor and the second may not redraw a fire the first already advanced past.
   * A per-display key needs a display id the renderer is not given today; a bubble
   * appearing on one screen instead of both is a far smaller fault than one that
   * cannot be dismissed.
   */
  const cursorRef = useRef(readStoredCursor())
  const dismissRef = useRef<number | null>(null)
  /**
   * The live "tell me when sessions are done" preference.
   *
   * A ref, not state: the session watcher below owns a socket that must outlive
   * re-renders, so it reads the answer at completion time rather than capturing a
   * value at subscribe time. Capturing would mean the switch only took effect after a
   * restart — and this is the switch that spent this whole port controlling nothing.
   */
  const sessionAlertsRef = useRef(true)
  /**
   * The dress-up prop the user picked, and a transient one worn only for a
   * celebration.
   *
   * Two values rather than one, because the celebrate prop must OVERLAY the saved
   * choice and then give it back — it never writes to config. `celebrateProp ??
   * savedProp` is the single answer both the art layer and the eye-suppression rule
   * read, so they cannot disagree about what is being worn.
   */
  /** Bubble identity for locally-raised bubbles; see the session watcher below. */
  const localSeqRef = useRef(0)
  /** The prop the user picked in the gallery. */
  const [savedProp, setSavedProp] = useState<GhostAccessory>('none')
  /** A prop worn only for the length of a celebration; overlays the saved one. */
  const [celebrateProp, setCelebrateProp] = useState<GhostAccessory | null>(null)
  const celebrateTimerRef = useRef<number | null>(null)
  /**
   * The live poll, exposed so the backend's fire doorbell can run it at once.
   *
   * A ref rather than lifting `poll` out of its effect: it closes over the cursor
   * and the slot bookkeeping, and duplicating that for a second caller is how two
   * drains end up disagreeing about what has been shown.
   */
  const pollNowRef = useRef<(() => void) | null>(null)


  const openPanel = useCallback(() => {
    setPanelOpen(true)
    // Screen coordinates: the overlay covers its whole display, so its client
    // coordinates ARE screen coordinates offset by the display origin, which the
    // main process resolves from the point it is given.
    window.crewCompanion?.panelOpen?.({
      x: Math.round(window.screenX + pos.x),
      y: Math.round(window.screenY + pos.y),
      width: PET_PX,
      height: PET_PX,
    })
    // The overlay is deliberately non-focusable so it never steals focus from the
    // user's work — but the reminder input needs the keyboard, so focus is granted
    // only while the panel is open and withdrawn the moment it closes.
    window.crewCompanion?.setFocusable(true)
  }, [pos.x, pos.y])

  /**
   * The pending poll below is armed once in a mount-time effect, so referencing
   * `openPanel` there directly would freeze it at the first render — including
   * the companion's start position, so a command arriving after the user has
   * dragged the companion would open the panel at the wrong place. A ref keeps it
   * current without re-arming the poll.
   */
  const openPanelRef = useRef(openPanel)
  openPanelRef.current = openPanel

  /**
   * Follow the panel window's own lifecycle.
   *
   * It closes on click-away, Escape and its ✕ without going through `closePanel`, so
   * without this the companion keeps believing the panel is open: the next click reads
   * as "close" and nothing appears, and the overlay stays focusable — a full-display
   * always-on-top window that can take focus and swallow clicks meant for other apps.
   */
  useEffect(() => {
    return window.crewCompanion?.onPanelClosed?.(() => {
      setPanelOpen(false)
      window.crewCompanion?.setFocusable(false)
    })
  }, [])

  const closePanel = useCallback(() => {
    setPanelOpen(false)
    window.crewCompanion?.panelClose?.()
    window.crewCompanion?.setFocusable(false)
  }, [])





  const dismiss = useCallback(() => {
    if (dismissRef.current !== null) {
      window.clearTimeout(dismissRef.current)
      dismissRef.current = null
    }
    /**
     * Dismissing frees the slot — for STICKY bubbles too.
     *
     * A sticky bubble has no ✕ and never auto-expires, but the whole bubble is a
     * dismiss target, so the user can still acknowledge one. Holding the slot after
     * that was a real bug: the bubble left the screen while the slot stayed taken for
     * up to STICKY_HOLD_MS, silently swallowing every notification that followed.
     *
     * A deliberate dismissal IS the acknowledgement the hold was waiting for, so it
     * releases the slot rather than leaving the 90s cap to do it.
     */
    slotRef.current = null
    setBubble(null)
    setPlacement(null)
  }, [])

  /**
   * Place the bubble directly above the companion using the ported algorithm.
   *
   * Measured, not estimated: the bubble is `width: fit-content`, so its real box is
   * known only after it renders. This runs in a layout effect (before paint, so no
   * flash), feeds the measured size to `pickBubblePlacement`, and clamps the result
   * with `resolveBubbleRect`. The three `random()` reads inside the picker are, in
   * order, jitterX, jitterY and the candidate choice — returning 0.5 twice zeroes the
   * jitter and 0 then selects the best-scoring (centred 'top') candidate, so the
   * bubble is reliably centred above the companion rather than randomly offset.
   */
  useLayoutEffect(() => {
    if (!bubble) return
    const el = bubbleHostRef.current
    if (!el) return
    const box = el.getBoundingClientRect()
    const width = box.width || BUBBLE_LAYOUT_DEFAULTS.maxWidth
    const height = box.height || 44
    const anchor: AnchorRect = {
      left: pos.x,
      top: pos.y,
      right: pos.x + PET_PX,
      bottom: pos.y + PET_PX,
      width: PET_PX,
      height: PET_PX,
    }
    let call = 0
    const random = () => (call++ < 2 ? 0.5 : 0)
    const candidate = pickBubblePlacement(
      anchor,
      width,
      height,
      window.innerWidth,
      window.innerHeight,
      { random },
    )
    const rect = resolveBubbleRect(
      candidate,
      width,
      height,
      window.innerWidth,
      window.innerHeight,
      BUBBLE_LAYOUT_DEFAULTS.margin,
    )
    /*
     * Keep the arrow off the rounded corners.
     *
     * `targetX` points at the companion, and when the companion is near a screen edge
     * the box is clamped inward — so the raw offset can land the arrow on the 14px
     * radius, where it reads as a stray notch rather than a pointer. The source clamps
     * to the same inset it uses for screen margins.
     */
    const arrowInset = BUBBLE_LAYOUT_DEFAULTS.margin
    const rawArrowX = candidate.targetX - rect.left
    const span = rect.right - rect.left
    const arrowX = Math.max(arrowInset, Math.min(span - arrowInset, rawArrowX))
    setPlacement({ rect, arrowX })
  }, [bubble, pos.x, pos.y])

  /*
   * Session completions, taken straight from the gateway.
   *
   * Everything else the companion says arrives by polling its own backend. This one
   * cannot: the backend has no idea a chat session exists, which is why the panel's
   * "Tell me when sessions are done" switch saved a value that nothing read. The
   * signal lives one layer up, on the dashboard WebSocket, and an app window is a
   * first-class client of it — the same route Mochi's panel already takes.
   *
   * Raised through `nextBubble` rather than `setBubble` directly, so a completion
   * obeys the same slot rules as everything else: it collapses into a count when
   * several land together, and it never shoves aside unresolved work that is holding
   * the slot. The reaction matches the completion branch of the poll below.
   */
  /*
   * Wear a random prop for one celebration.
   *
   * Picked fresh each time so repeated completions do not look canned, and 'none'
   * is IN that set on purpose — a plain hop has to stay a common outcome, or every
   * finish turns into confetti and the flourish stops meaning anything.
   *
   * Held 450ms past the 900ms hop so the prop does not vanish mid-bounce.
   */
  const celebrateWithProp = useCallback(() => {
    if (celebrateTimerRef.current !== null) window.clearTimeout(celebrateTimerRef.current)
    setCelebrateProp(randomCelebrateProp())
    celebrateTimerRef.current = window.setTimeout(() => {
      setCelebrateProp(null)
      celebrateTimerRef.current = null
    }, CELEBRATE_MS + CELEBRATE_PROP_HOLD_MS)
  }, [])

  useEffect(() => watchSessions({
    isSilent: () => sessionAlertsRef.current === false,
    // The backend rang: drain now rather than at the next tick of the poll.
    onFireQueued: () => pollNowRef.current?.(),
    onDone: ({ title, failed }) => {
      /*
       * A failure is a DIFFERENT notification, not a finish with sad wording.
       *
       * The gateway ends a broken turn with an ordinary `chat_done`, so treating
       * every completion as success meant the companion celebrated work that had
       * actually stopped. The kind drives the wording, the body reaction, the face
       * and the CTA, so it has to be decided here.
       */
      const kind: NotifKind = failed ? 'session-error' : 'session-done'
      // A session's title is the user's own words — shown verbatim, never translated.
      // Only the fallback for an untitled session is copy.
      /*
       * A failure has to SAY so.
       *
       * On success the session's title stands alone — those are the user's words. On
       * failure the bare title reads exactly like a finish, so it is prefixed the way
       * the source prefixes it ("Stopped: <title>"), and an untitled failure gets its
       * own sentence rather than an empty one.
       */
      const named = title.trim()
      const text = failed
        ? (named
          ? i18nT('apps.crewCompanion.notif.stoppedNamed', { name: named })
          : i18nT('apps.crewCompanion.notif.taskStopped'))
        : (named || i18nT('apps.crewCompanion.notif.finishedTask'))
      const now = Date.now()
      const held = slotRef.current
      if (held?.sticky && now - held.at < STICKY_HOLD_MS) return
      const result = nextBubble(
        slotRef.current, { text, sticky: isSticky(kind), kind }, now,
      )
      slotRef.current = result.pending
      if (result.show === null) return
      // Local, negative sequence numbers: these bubbles have no backend fire behind
      // them, and a positive number could collide with a real fire's seq.
      localSeqRef.current -= 1
      setBubble({
        seq: localSeqRef.current,
        kind: result.pending?.kind ?? kind,
        text: result.show,
      })
      if (failed) {
        react('error', 2_000)
        setMood('scared')
      } else {
        react('done', 2_400)
        setMood('happy')
        celebrateWithProp()
      }
    },
    /*
     * A tool is BLOCKED on your OK — raise the sticky approval bubble.
     *
     * This is the producer the page's promise ("anything waiting on you always
     * notifies") depended on and never had. It is additive: the resolver below
     * already knew how to CLEAR an approval bubble, so this closes the loop by
     * creating one.
     *
     * Built through the SAME slot path `onDone` uses, so it obeys the one-slot rule
     * and will not shove aside other unresolved work still holding it. Two things it
     * does NOT do, on purpose:
     *   - it never consults `isSilent`: that switch is about session-DONE good news,
     *     and per notificationPolicy an approval is unresolved work that always
     *     notifies — `isSticky('approval')` is true, so there is no ✕ and it leaves
     *     only by being resolved.
     *   - it reacts with a CURIOUS mood, not the error shake: waiting on you is a
     *     question, not a failure — the same choice the poll loop makes for
     *     'approval'/'session-input'.
     *
     * The words: the kind label ("Approval Pending") is the kicker and the session's
     * own title is the body, reusing the existing state.approval_pending string so no
     * new copy is minted; an untitled session shows the label alone.
     */
    onApproval: ({ title }) => {
      const kind: NotifKind = 'approval'
      const label = i18nT('apps.crewCompanion.state.approval_pending')
      const named = title.trim()
      // "kicker\nbody": Bubble treats a short first line as an upper-case label above
      // the body, so this reads as "APPROVAL PENDING" over the session's own words.
      const text = named ? `${label}\n${named}` : label
      const now = Date.now()
      const held = slotRef.current
      if (held?.sticky && now - held.at < STICKY_HOLD_MS) return
      const result = nextBubble(
        slotRef.current, { text, sticky: isSticky(kind), kind }, now,
      )
      slotRef.current = result.pending
      if (result.show === null) return
      // Local, negative sequence: no backend fire sits behind this bubble, and a
      // positive number could collide with a real fire's seq.
      localSeqRef.current -= 1
      setBubble({
        seq: localSeqRef.current,
        kind: result.pending?.kind ?? kind,
        text: result.show,
      })
      // Curious, not alarmed: activeAnimFor turns a curious mood into the head-cock.
      bumpReaction()
      setMood('curious')
    },
    /*
     * Blocked work answered elsewhere frees the slot at once.
     *
     * The bubble asked a question; once it is answered in the dashboard the bubble is
     * stale, and leaving it to the bounded hold means everything behind it waits on
     * something already decided.
     */
    onApprovalResolved: () => {
      if (slotRef.current?.sticky) slotRef.current = null
      setBubble((b) => (b && isSticky(b.kind) ? null : b))
    },
  }), [react, setMood, celebrateWithProp, bumpReaction])

  /** Presence: silence is read as "nobody is there", so this must not stop. */  useEffect(() => {
    void post(PRESENCE_PATH)
    const t = window.setInterval(() => void post(PRESENCE_PATH), PRESENCE_MS)
    return () => window.clearInterval(t)
  }, [])

  /** Collect what fired. Cursor-based, so a dropped response loses nothing. */
  useEffect(() => {
    let stopped = false

    const poll = async () => {
      try {
        const since = cursorRef.current
        const r = await fetch(`${PENDING_PATH}?since=${since}`, {
          credentials: 'same-origin',
        })
        if (!r.ok) return
        let data = (await r.json()) as { cursor: number; fires: Fire[] }
        if (stopped) return

        /*
         * A cursor BELOW the one we asked from means the gateway restarted.
         *
         * The sequence lives in memory on the backend and begins again at zero, so
         * a persisted cursor of 42 outlives it: the next reminder fires as seq 1,
         * `seq > 42` excludes it, and the row is already marked done — the nudge is
         * gone for good, silently. Persisting the cursor is what stops a restart
         * REPLAYING old bubbles; this is the other half of that trade, and it has
         * to break the same way round: re-read from zero and risk showing one thing
         * twice rather than swallow something the user asked to be told.
         */
        if (data.cursor < since) {
          const again = await fetch(`${PENDING_PATH}?since=0`, { credentials: 'same-origin' })
          if (!again.ok) return
          data = (await again.json()) as { cursor: number; fires: Fire[] }
          if (stopped) return
        }

        // Window commands the dashboard page recorded (Open panel / Change
        // avatar). The page has no bridge of its own, so it enqueues the intent
        // in the backend and this overlay — which does hold the bridge — carries
        // it out on the poll it already runs. Acted on, never drawn: a command
        // carries no bubble text, and a stale one is skipped (see COMMAND_FRESH_MS).
        for (const f of data.fires) {
          if (f.kind !== 'command') continue
          const ts = Date.parse(f.at)
          if (Number.isFinite(ts) && Date.now() - ts > COMMAND_FRESH_MS) continue
          if (f.text === 'panel') openPanelRef.current()
          else if (f.text === 'gallery') window.crewCompanion?.galleryOpen?.()
        }

        /*
         * One bubble at a time, and the cursor only moves past what was SHOWN.
         *
         * Two reminders coming due in the same tick arrive in one batch. Taking
         * the newest and advancing the cursor to the end of the batch showed one
         * and silently dropped the other — the row is already marked done, so it
         * never comes back. Nothing surfaces the loss; the user simply never hears
         * about the thing they asked to be reminded of.
         *
         * So: take the OLDEST unspoken fire, and leave the cursor at its sequence
         * so the rest are still pending on the next poll two seconds later. They
         * then appear in the order they came due, which is also the order the
         * desktop app showed them in when its IPC delivered them one by one.
         * Commands are consumed above regardless, since acting on one twice would
         * reopen a window the user just closed.
         */
        const speakable = data.fires.filter((f) => f.kind !== 'command')
        /*
         * ONE commit point for the cursor — this is the third cursor bug, so the
         * rule is now structural rather than another patch.
         *
         * The first two were both "the cursor moved past something never shown"
         * (newest-of-batch, restart replay). The third: advancing BEFORE the
         * display decision meant any later bail-out — a sticky hold, an unmapped
         * nudge key — consumed the fire silently. A due reminder arriving while
         * an approval bubble held the slot was marked done and never heard from.
         *
         * So the advance happens only through commitCursor, invoked exactly where
         * a fire's fate is decided: SHOWN, or DELIBERATELY consumed. A fire that
         * is neither (deferred by a sticky hold) leaves the cursor untouched and
         * the 2s poll retries it until the hold clears — nextBubble keeps no
         * internal queue (show:null drops the incoming), so the unmoved cursor IS
         * the retry mechanism.
         */
        const commitCursor = (seq: number) => {
          cursorRef.current = seq
          writeStoredCursor(seq)
        }
        if (speakable.length === 0) {
          commitCursor(data.cursor)
          return
        }
        const latest = speakable[0]

        const kind = (latest.kind || 'other') as NotifKind
        // A break nudge names its phrasing by catalogue key so it can be
        // translated here; a reminder carries the user's own words, which must
        // never be run through translation.
        let text = latest.text
        if (latest.key) {
          const phrase = nudgeTextFor(latest.key)
          // An unmapped key means backend and catalogue have drifted. Staying
          // silent beats showing someone the string "break.water.3". CONSUMED
          // deliberately (cursor committed), not deferred: drift does not heal
          // in two seconds, and retrying would pin the whole queue behind it.
          if (!phrase) {
            commitCursor(latest.seq)
            return
          }
          text = phrase
        }

        // One slot, never a queue. Ambient nudges (break / reminder) show as-is
        // but never displace blocked work that is holding the slot; completions
        // collapse into a running count; blocked / needs-input work is sticky and
        // holds the slot for a bounded window (STICKY_HOLD_MS).
        const now = Date.now()
        const isAmbient = kind === 'break' || kind === 'break-breathe' || kind === 'reminder'
        if (isAmbient) {
          const held = slotRef.current
          // DEFERRED, not consumed: the cursor stays put, so the next poll
          // re-delivers this fire once the sticky window lapses.
          if (held?.sticky && now - held.at < STICKY_HOLD_MS) return
          commitCursor(latest.seq)
          slotRef.current = { text, sticky: false, count: 1, at: now, kind }
          setBubble({ seq: latest.seq, kind, text })
          return
        }

        // One definition, shared with the bubble UI: the slot's notion of sticky and
        // the ✕'s must not drift apart.
        const sticky = isSticky(kind)
        const result = nextBubble(slotRef.current, { text, sticky, kind }, now)
        // show === null is nextBubble's "deferred by a sticky hold" verdict: the
        // slot is unchanged and the incoming bubble was dropped, so the cursor
        // must not move — the unmoved cursor is what brings the fire back.
        if (result.show === null) return
        commitCursor(latest.seq)
        slotRef.current = result.pending
        const shownKind = result.pending?.kind ?? kind
        setBubble({ seq: latest.seq, kind: shownKind, text: result.show })
        // A finish is a celebration; a failure or something blocked is a shake. Both
        // settle back to idle so the companion does not sit in a reaction.
        // The body reacts AND the mood changes, because the mood is what the eyes
        // read — a body nod alone left the face blank, which is why the companion
        // looked unmoved by its own notifications. Transient, so it auto-resets.
        if (shownKind === 'session-error') {
          react('error', 2_000)
          setMood('scared')
        } else if (shownKind === 'approval' || shownKind === 'session-input') {
          /*
           * Waiting on YOU is not a failure.
           *
           * These three kinds used to share the alarmed error shake, which made
           * "something broke" and "I need your OK" look identical. The source cocks
           * the head instead — curious, not alarmed — and the mood is what drives it,
           * so no `react` here: `activeAnimFor` turns a curious mood into the head-cock.
           */
          bumpReaction()
          setMood('curious')
        } else {
          react('done', 2_400)
          setMood('happy')
          // Every finish gets the flourish, not just the ones arriving over the socket.
          celebrateWithProp()
        }
      } catch {
        /* keep polling */
      }
    }

    void poll()
    pollNowRef.current = () => void poll()
    const t = window.setInterval(() => void poll(), PENDING_MS)
    return () => {
      pollNowRef.current = null
      stopped = true
      window.clearInterval(t)
    }
  }, [])


  // ── Local aliveness ─────────────────────────────────────────────────────
  // The companion stays at its home spot and only does small nearby fidgets (a
  // little hop out and straight back) or brief mood flickers — it never roams the
  // screen (see useIdleFidget). A hop animates `pos` within the overlay via
  // useWalking rather than moving a window; when a hop ENDS back near an edge, this
  // decides whether the companion has come to rest against it and should dock. That
  // end-of-walk edge check is inlined here exactly as the desktop app's PetWidget
  // wrote it (the 40px threshold), so a hop settling at the edge tucks in.
  const handleWalkEnd = useCallback((finalPos: { x: number; y: number }) => {
    const edgeThreshold = 40
    const atLeft = finalPos.x <= edgeThreshold
    const atRight = finalPos.x >= window.innerWidth - PET_PX - edgeThreshold
    if (atLeft || atRight) {
      setHideEdge(atLeft ? 'left' : 'right')
      setIsPeeking(true)
    } else {
      setIsPeeking(false)
      setHideEdge(null)
    }
    // Persist through the same petX/petY config path a drag uses.
    petBridge.savePosition?.(finalPos.x, finalPos.y)
  }, [setHideEdge, setIsPeeking])

  const { isWalking, walkDir, walkTilt, cancelWalk, walkPath } =
    useWalking(pos, setPos, handleWalkEnd, setIsPeeking, setHideEdge)

  /**
   * Everything that must be quiet for the companion to move on its own, evaluated at
   * render (each is state-backed, so a change re-renders and refreshes the gate). A
   * fidget never begins while the user is dragging, the companion is walking or
   * docked, the quick-menu is up, or the OS asks for reduced motion — matching how
   * the desktop app gated useIdleFidget, plus the reduced-motion honour.
   */
  const reducedMotion =
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  const settled =
    posReady && !dragging.current && !isWalking && !isPeeking &&
    menuAt === null && !reducedMotion

  // Built-in ghost: small in-place hop / brief mood flicker.
  /*
   * The idle fidget currently playing, if any.
   *
   * Held in state rather than derived, because a fidget is something the companion
   * DECIDED to do at a random moment -- nothing about the current state or mood
   * implies it. It clears itself after the motion's own length so the companion
   * returns to still without needing another signal.
   */
  const [idleAnim, setIdleAnim] = useState<PetAnim>(null)
  const idleAnimTimer = useRef<number | null>(null)

  const playFidget = useCallback((anim: PetAnim, holdMs: number) => {
    if (idleAnimTimer.current !== null) window.clearTimeout(idleAnimTimer.current)
    // Same epoch bump as a reaction: without it, drawing the SAME fidget twice in a
    // row would reuse the DOM node and the keyframes would never restart.
    bumpReaction()
    setIdleAnim(anim)
    idleAnimTimer.current = window.setTimeout(() => setIdleAnim(null), holdMs)
  }, [bumpReaction])

  useEffect(() => () => {
    if (idleAnimTimer.current !== null) window.clearTimeout(idleAnimTimer.current)
  }, [])

  /*
   * The author-named random clip currently playing on a CUSTOM pack, if any.
   *
   * The custom-pack counterpart of `idleAnim`: `useRandomClips` picks a name from the
   * pack's own extras (wave / waiting / run for a PetDex import), and this holds it
   * while it plays. Duration is a fixed hold rather than the clip's own length,
   * because a sprite strip loops forever — there is no "end" to wait for. Cleared
   * back to the state slot afterwards.
   */
  const [activeClip, setActiveClip] = useState<string | undefined>(undefined)
  const clipTimer = useRef<number | null>(null)
  const CLIP_HOLD_MS = 3_000

  const playExtraClip = useCallback((name: string) => {
    if (clipTimer.current !== null) window.clearTimeout(clipTimer.current)
    // Same epoch bump as every other one-off motion: repeating the SAME clip must
    // remount and replay, not silently reuse the DOM node.
    bumpReaction()
    setActiveClip(name)
    clipTimer.current = window.setTimeout(() => setActiveClip(undefined), CLIP_HOLD_MS)
  }, [bumpReaction])

  useEffect(() => () => {
    if (clipTimer.current !== null) window.clearTimeout(clipTimer.current)
  }, [])

  useIdleFidget({
    enabled: isDefaultPack && settled,
    getPos: () => pos,
    walkPath,
    setMood,
    playFidget,
  })

  // Custom packs: only the random content the pack itself ships.
  useRandomClips({
    enabled: !isDefaultPack && settled,
    getBehaviors: () => customBehaviors,
    getPos: () => pos,
    walkPath,
    setMood,
    playExtra: playExtraClip,
  })

  // Report the companion's and bubble's hitboxes to the main process; it polls the
  // cursor at ~60fps and toggles this overlay's click-through itself. The context
  // menu reports its own rect separately (PetContextMenu → petBridge.setMenuHitbox).
  // `placement` is null until the bubble is measured, so the bubble rect is reported
  // once it lands.
  useMouseForward({ pos, bubbleRect: placement?.rect ?? null, dragging })

  // Playful motion runs only when the companion is settled — not while it is being
  // dragged, walking, or docked at an edge. Set every render so the rAF loop sees
  // current state.
  playActiveRef.current = !dragging.current && !isPeeking && !isWalking

  // Mirror on the right half so the art faces the screen. Lifted out of the style
  // block so the eye gaze can share it — eyes and body then agree on which way is
  // "toward the cursor". A walk flips the art to face its direction of travel.
  // Set every render so the rAF loop sees the current facing (the file uses the same
  // approach for its other motion flags).
  const facingRight =
    (isWalking && walkDir < 0) ||
    (!isWalking && hideEdge === 'right') ||
    (!isWalking && !hideEdge && pos.x > window.innerWidth / 2)

  facingRightRef.current = facingRight

  /**
   * Which body motion is playing, by the desktop app's own precedence:
   * error > celebrate > curious > fly > ponder (see petAnim). Only the live pet can
   * decide this — the choice depends on travelling, mood and docking, none of which a
   * bare state expresses, which is why it is computed here and handed to PetAvatar.
   */
  const activeAnim = activeAnimFor({
    state: petState,
    mood,
    docked: hideEdge !== null,
    walking: isWalking,
    idleAnim,
  })

  return (
    <div className="cc-pet-layer">
      {menuAt ? (
        <div className="cc-menu-host">
          <PetContextMenu
            x={menuAt.x}
            y={menuAt.y}
            isHidden={isPeeking}
            onClose={() => setMenuAt(null)}
          />
        </div>
      ) : null}

      {bubble ? (
        <div
          ref={bubbleHostRef}
          className="cc-bubble-host"
          style={
            placement
              ? { position: 'absolute', left: placement.rect.left, top: placement.rect.top }
              : // Until measured, render off-screen and invisible so getBoundingClientRect
                // reads the real box; the layout effect then places it before paint.
                { position: 'absolute', left: -9999, top: -9999, visibility: 'hidden' }
          }
        >
          <Bubble
            text={bubble.text}
            kind={bubble.kind}
            onDismiss={dismiss}
            onAction={(action) => {
              // The breathing nudge's CTA opens the exercise, which is the whole
              // reason that bubble carries one.
              // The exercise lives in the panel window, so the CTA opens the panel
              // and the window starts it from there.
              if (action === 'breathe') openPanel()
            }}
          />
        </div>
      ) : null}

      {/* The only element that accepts input; everything else is click-through. */}
      <div
        className="cc-pet"
        onMouseDown={(e) => {
          clickDownPt.current = { x: e.clientX, y: e.clientY }
          /*
           * Hold the panel open from the PRESS, not from `isDragging`.
           *
           * The panel closes on blur like a popover, and grabbing the companion focuses
           * the overlay — which blurs the panel immediately. `isDragging` only turns
           * true once the pointer crosses the drag threshold, so holding on it arrived
           * after the panel had already gone. Grabbing the companion is not "clicking
           * elsewhere", so the hold starts here and is released on mouseup.
           */
          window.crewCompanion?.panelHold?.(true)
          if (isWalking) cancelWalk()
          onMouseDown(e)
        }}
        onContextMenu={(e) => {
          e.preventDefault()
          setMenuAt({ x: e.clientX, y: e.clientY })
        }}
        style={{
          position: 'absolute',
          left: pos.x,
          top: pos.y,
          width: PET_PX,
          height: PET_PX,
          cursor: dragging.current ? 'grabbing' : 'grab',
          // Hidden until the saved position arrives, so the companion does not
          // appear in the default corner and then jump to where the user left it.
          opacity: posReady ? 1 : 0,
          transition: isDragging ? 'none' : 'opacity .4s ease, transform .3s ease',
          // Face the middle of the screen: the art points one way, so on the right
          // half it has to be mirrored or the companion looks off the edge.
          transform: (() => {
            const parts: string[] = []
            // Mirror on the right half so the art faces the screen, not the edge.
            if (facingRight) parts.push('scaleX(-1)')
            // A diagonal walk leg tilts the body slightly (±6°), from useWalking.
            if (isWalking && walkTilt !== 0) parts.push(`rotate(${walkTilt}deg)`)
            // Docked at an edge: tuck half the body off-screen. Present, but out of
            // the way of whatever the user is working on.
            // Docked: half the body slides off-screen and the companion tilts, so it
            // reads as leaning against the edge rather than being clipped by it.
            // DOCK_CROP = half the width, DOCK_ROTATE = 25°, from PetWidget.
            if (isPeeking && hideEdge) {
              // Tuck half the body off the docked edge. The crop is expressed as a
              // desired SCREEN offset, then flipped when the art is mirrored: on the
              // right edge `facingRight` has already pushed scaleX(-1), which would
              // otherwise invert a raw translateX and slide the body INWARD instead
              // of off-screen (why the right-edge dock never looked docked).
              const dockScreenDx = hideEdge === 'left' ? -PET_PX / 2 : PET_PX / 2
              parts.push(`translateX(${facingRight ? -dockScreenDx : dockScreenDx}px)`)
              parts.push(`rotate(${DOCK_ROTATE}deg)`)
            }
            return parts.length ? parts.join(' ') : undefined
          })(),
        }}
        role="button"
        tabIndex={0}
        aria-label={i18nT('apps.crewCompanion.panel.breathe.title')}
        onClick={(e) => {
          // Only a genuine tap counts. A drag also fires a click on release, so
          // compare against where the press started — past CLICK_SLOP it was a drag
          // and the panel must not open.
          const down = clickDownPt.current
          const moved = down ? Math.hypot(e.clientX - down.x, e.clientY - down.y) : 999
          if (moved > CLICK_SLOP) return
          poke()
          if (panelOpen) closePanel(); else openPanel()
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            if (panelOpen) closePanel(); else openPanel()
          }
        }}
      >
        {/*
          The real mascot art from the Kiro Design System, the same asset the chat
          loading carousel uses — NOT a hand-drawn SVG. `use-lucide-icons` in
          website/AUTOSDE.yaml blocks inline SVG elements in any .tsx
          unconditionally, and its brand-mark exception requires exactly this: the
          mark lives in its own file and is consumed through a URL import.
        */}
        {/*
          The full avatar: the active appearance pack, its format (svg, sprite or
          Lottie), the user's recolouring, and the motion for the current state.
          Replaces the static image this used to be.
        */}
        {/*
          The art wrapper `usePlayfulMotion` drives. It sits INSIDE the flip/dock
          transform above (on .cc-pet) and carries only the playful translate, so the
          two never fight — exactly the nesting the desktop app's PetWidget uses. The
          state keyframes (ponder/celebrate/error) live one level deeper, on PetAvatar's
          own element, so they compose with this translate rather than overwrite it.
        */}
        <div
          ref={artRef}
          style={{ transformOrigin: '50% 82%', willChange: 'transform', width: PET_PX, height: PET_PX }}
        >
          <PetAvatar
            size={PET_PX}
            state={petState}
            mood={mood}
            docked={hideEdge !== null}
            anim={activeAnim}
            animEpoch={reactionEpoch}
            /*
             * A playing clip must never mask a real reaction: done/error outrank it.
             * `petState` returning to idle is what lets the held clip show.
             */
            clipName={petState === 'idle' ? activeClip : undefined}
            trackCursor
            flipX={facingRight}
            accessory={celebrateProp ?? savedProp}
          />
        </div>
      </div>
    </div>
  )
}



// Before the first render, so nothing paints a bare key.
initI18n()

const host = document.getElementById('companion-root')
if (host) {
  // Await the theme before the first paint: the panel is styled entirely from
  // Kiro Crew's variables, and rendering ahead of them shows fallback colours and
  // then snaps to the real ones.
  void adoptDashboardTheme().then(() => {
    watchThemeChanges()
    createRoot(host).render(
      <StrictMode>
        <Companion />
      </StrictMode>,
    )
  })
}
