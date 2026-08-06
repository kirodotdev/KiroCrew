import { useCallback, useEffect, useRef, useState } from 'react'
import { AppWindow, Maximize2, Minimize2, Minus, X } from 'lucide-react'

import { useComputerUseFrame, COMPUTER_USE_FRAME_EVENT } from '../hooks/useComputerUseFrame'
import { safeSetItem } from '../utils/safeStorage'

import { i18nT } from '../i18n/t'
import { fmtTimeNumeric } from '../i18n/format'

/**
 * ComputerUseLiveView — floating picture-in-picture view of the desktop the agent
 * is driving.
 *
 * The machine running computer use is often not the machine the operator is
 * looking at (a cloud Mac, a session reached over the reverse SSH tunnel, or just
 * another Space), so this panel is the window onto what the agent actually sees.
 *
 * It renders **relayed** frames only: each one is the downscaled JPEG the agent's
 * own `computer_get_state` call already captured and already received. Nothing
 * here requests a capture, so opening the panel cannot make the agent screenshot
 * anything, and the gateway drops a frame entirely when the window held a
 * password field or when the governance ceiling denies the `screenshot`
 * observation channel. "No frames" is therefore a normal state, not a fault, and
 * the empty body says so.
 *
 * Lifecycle: `hidden` → (first frame) `open` ⇄ `chip`. There is no toolbar
 * button — activity reveals it and the header dismisses it.
 * - Close (✕) dismisses this session's view without leaving a chip, and remembers
 *   the session so its later frames do not bounce the panel open again. A
 *   different driving session is new activity and still surfaces.
 * - Minimize (–) collapses to a corner chip, which is the re-open affordance.
 * - The window is a free rect: drag the header to move, drag any of the eight
 *   grips to resize, or use the header button to swap between the compact and
 *   large presets. Every geometry change is fitted back into the viewport so no
 *   grip can end up off-screen. The size persists in localStorage; the position
 *   re-docks to the bottom-left corner on each reveal (the browse mirror owns the
 *   bottom-right, so the two never overlap).
 *
 * Strictly read-only: there is no click-through, no input relay, and no control
 * channel. Driving the desktop happens through the governed MCP tools only.
 */

/** Panel lifecycle state. */
type Phase = 'hidden' | 'chip' | 'open'

/** A grip's compass direction; two-letter values drive both axes. */
export type Grip = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'

/** Top-left-anchored panel rect in CSS pixels. */
export interface Box {
  left: number
  top: number
  width: number
  height: number
}

/** Smallest usable panel: below this the header buttons start colliding. */
const MIN_WIDTH = 200
const MIN_HEIGHT = 140
/** Gap kept between the panel and every viewport edge, so grips stay grabbable. */
const EDGE_GAP = 16
/** Reveal size — a thumbnail that does not take the screen over. */
const COMPACT_SIZE = { width: 300, height: 210 }
/** Header-button preset for reading small UI text in the mirrored window. */
const ROOMY_SIZE = { width: 900, height: 620 }
const STORED_SIZE_KEY = 'mc-computer-mirror-box'

/** Grip placement + cursor. Corners paint over edges so a corner drag wins. */
const GRIP_STYLES: Record<Grip, { box: string; cursor: string; onTop: boolean }> = {
  n: { box: 'top-0 left-0 right-0 h-1.5', cursor: 'cursor-ns-resize', onTop: false },
  s: { box: 'bottom-0 left-0 right-0 h-1.5', cursor: 'cursor-ns-resize', onTop: false },
  w: { box: 'left-0 top-0 bottom-0 w-1.5', cursor: 'cursor-ew-resize', onTop: false },
  e: { box: 'right-0 top-0 bottom-0 w-1.5', cursor: 'cursor-ew-resize', onTop: false },
  nw: { box: 'top-0 left-0 h-3 w-3', cursor: 'cursor-nwse-resize', onTop: true },
  ne: { box: 'top-0 right-0 h-3 w-3', cursor: 'cursor-nesw-resize', onTop: true },
  sw: { box: 'bottom-0 left-0 h-3 w-3', cursor: 'cursor-nesw-resize', onTop: true },
  se: { box: 'bottom-0 right-0 h-3 w-3', cursor: 'cursor-nwse-resize', onTop: true },
}

const GRIP_ORDER: Grip[] = ['n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se']

/**
 * Catalog KEY for each grip's full aria-label, so every handle is addressable by
 * name in tests and by a screen reader.
 *
 * Keys, not strings, and the WHOLE label rather than a compass word interpolated
 * into an English frame: this table is evaluated at module load, so an `i18nT()`
 * call here would freeze the boot language, and a `Resize … (${name})` template
 * would hand translators a fragment with no control over the phrasing around it.
 * The lookup happens in the render callback below. Flat `Record` of full literal
 * keys indexed inline at the `i18nT()` call, because that is the form
 * `scripts/check-i18n-keys.mjs` can resolve statically.
 */
const GRIP_LABEL_KEY: Record<Grip, string> = {
  n: 'components.computerUseLiveView.resize_live_desktop_view_top',
  s: 'components.computerUseLiveView.resize_live_desktop_view_bottom',
  w: 'components.computerUseLiveView.resize_live_desktop_view_left',
  e: 'components.computerUseLiveView.resize_live_desktop_view_right',
  nw: 'components.computerUseLiveView.resize_live_desktop_view_top_left',
  ne: 'components.computerUseLiveView.resize_live_desktop_view_top_right',
  sw: 'components.computerUseLiveView.resize_live_desktop_view_bottom_left',
  se: 'components.computerUseLiveView.resize_live_desktop_view_bottom_right',
}

const between = (low: number, value: number, high: number): number =>
  value < low ? low : value > high ? high : value

/** The rect the panel may occupy: the viewport inset by EDGE_GAP on all sides. */
function playfield(): { minX: number; minY: number; maxX: number; maxY: number } {
  return {
    minX: EDGE_GAP,
    minY: EDGE_GAP,
    maxX: Math.max(EDGE_GAP + MIN_WIDTH, window.innerWidth - EDGE_GAP),
    maxY: Math.max(EDGE_GAP + MIN_HEIGHT, window.innerHeight - EDGE_GAP),
  }
}

/**
 * Fit `box` inside the playfield: shrink it to what fits, then slide it until no
 * edge is outside. Applied after every move, resize, preset swap and viewport
 * change, so the panel and all eight grips always stay reachable.
 */
export function fitToViewport(box: Box): Box {
  const field = playfield()
  const width = between(MIN_WIDTH, box.width, Math.max(MIN_WIDTH, field.maxX - field.minX))
  const height = between(MIN_HEIGHT, box.height, Math.max(MIN_HEIGHT, field.maxY - field.minY))
  return {
    width,
    height,
    left: between(field.minX, box.left, Math.max(field.minX, field.maxX - width)),
    top: between(field.minY, box.top, Math.max(field.minY, field.maxY - height)),
  }
}

/**
 * Park a panel of this size in the bottom-LEFT corner — the reveal position.
 * Deliberately the opposite corner from the browse mirror so both live views can
 * be open at once without stacking.
 */
export function dockBox(size: { width: number; height: number }): Box {
  const field = playfield()
  const width = between(MIN_WIDTH, size.width, Math.max(MIN_WIDTH, field.maxX - field.minX))
  const height = between(MIN_HEIGHT, size.height, Math.max(MIN_HEIGHT, field.maxY - field.minY))
  return { left: field.minX, top: Math.max(field.minY, field.maxY - height), width, height }
}

/**
 * Apply a grip drag to `box`.
 *
 * Works in edge space (left/top/right/bottom) rather than position+size: the
 * dragged edges move, the others are untouched by definition, and a drag that
 * would invert the rect is stopped by clamping the moved edge against its
 * opposite one plus the minimum. Also clamps to the playfield, so a resize can
 * never push the panel out of view.
 */
export function applyGrip(box: Box, grip: Grip, dx: number, dy: number): Box {
  const field = playfield()
  let west = box.left
  let north = box.top
  let east = box.left + box.width
  let south = box.top + box.height

  if (grip.includes('w')) west = between(field.minX, west + dx, east - MIN_WIDTH)
  if (grip.includes('e')) east = between(west + MIN_WIDTH, east + dx, field.maxX)
  if (grip.includes('n')) north = between(field.minY, north + dy, south - MIN_HEIGHT)
  if (grip.includes('s')) south = between(north + MIN_HEIGHT, south + dy, field.maxY)

  return { left: west, top: north, width: east - west, height: south - north }
}

/** Read the persisted panel size, falling back to the compact preset. */
function storedSize(): { width: number; height: number } {
  try {
    const raw = localStorage.getItem(STORED_SIZE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (typeof parsed?.width === 'number' && typeof parsed?.height === 'number') {
        return {
          width: Math.max(MIN_WIDTH, parsed.width),
          height: Math.max(MIN_HEIGHT, parsed.height),
        }
      }
    }
  } catch {
    /* a malformed persisted size is not worth surfacing — use the default */
  }
  return { ...COMPACT_SIZE }
}

/** An in-flight pointer gesture: either a header move or one grip's resize. */
interface Gesture {
  originX: number
  originY: number
  startBox: Box
  grip: Grip | null
}

export default function ComputerUseLiveView() {
  const [phase, setPhase] = useState<Phase>('hidden')
  const [box, setBox] = useState<Box>(() => dockBox(storedSize()))
  const { frame, lastTs, sessionKey, sessionName, appName } = useComputerUseFrame()
  const gestureRef = useRef<Gesture | null>(null)
  // Session whose view the operator explicitly closed. Its further frames must
  // not re-open the panel; a different session clears it (see the frame effect).
  const dismissedRef = useRef<{ session: string | null } | null>(null)

  // Persist size only. Position is intentionally not persisted: the panel re-docks
  // to its corner on every reveal, which is predictable regardless of where the
  // operator last dragged it or how the viewport has since changed.
  useEffect(() => {
    safeSetItem(STORED_SIZE_KEY, JSON.stringify({ width: box.width, height: box.height }))
  }, [box.width, box.height])

  // Keep the panel reachable when the viewport shrinks (window resize, dev tools).
  useEffect(() => {
    const refit = () => setBox(current => fitToViewport(current))
    window.addEventListener('resize', refit)
    return () => window.removeEventListener('resize', refit)
  }, [])

  // Reveal on the first frame of a driving session. This effect owns the PHASE
  // only — the frame bytes and titles belong to useComputerUseFrame — so a frame
  // arriving while the panel is open or collapsed just updates the image.
  useEffect(() => {
    const onFrame = (event: Event) => {
      const detail = (event as CustomEvent<{ data?: string; session_key?: string }>).detail
      if (!detail?.data) return
      const incoming = detail.session_key || null
      setPhase(current => {
        if (current !== 'hidden') return current
        const dismissed = dismissedRef.current
        if (dismissed) {
          if (dismissed.session === incoming) return 'hidden'
          dismissedRef.current = null
        }
        setBox(current2 => dockBox({ width: current2.width, height: current2.height }))
        return 'open'
      })
    }
    window.addEventListener(COMPUTER_USE_FRAME_EVENT, onFrame)
    return () => window.removeEventListener(COMPUTER_USE_FRAME_EVENT, onFrame)
  }, [])

  // Programmatic open⇄chip toggle, for a future command-palette entry or shortcut.
  // Also how a reviewer can inspect the pre-first-frame empty state.
  useEffect(() => {
    const onToggle = () => setPhase(current => (current === 'open' ? 'chip' : 'open'))
    window.addEventListener('kirocrew-toggle-computer-use-live', onToggle)
    return () => window.removeEventListener('kirocrew-toggle-computer-use-live', onToggle)
  }, [])

  // One gesture pipeline for both moving and resizing, driven by WINDOW-level
  // listeners attached for the duration of the drag. Window listeners (rather
  // than per-element pointer capture) mean a fast drag that outruns the pointer
  // still tracks, and releasing outside the panel still ends the gesture.
  const beginGesture = useCallback(
    (grip: Grip | null) => (event: React.PointerEvent) => {
      event.preventDefault()
      event.stopPropagation()
      gestureRef.current = {
        originX: event.clientX,
        originY: event.clientY,
        startBox: box,
        grip,
      }
    },
    [box],
  )

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      const gesture = gestureRef.current
      if (!gesture) return
      const dx = event.clientX - gesture.originX
      const dy = event.clientY - gesture.originY
      setBox(
        gesture.grip
          ? fitToViewport(applyGrip(gesture.startBox, gesture.grip, dx, dy))
          : fitToViewport({
              ...gesture.startBox,
              left: gesture.startBox.left + dx,
              top: gesture.startBox.top + dy,
            }),
      )
    }
    const onRelease = () => {
      gestureRef.current = null
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onRelease)
    window.addEventListener('pointercancel', onRelease)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onRelease)
      window.removeEventListener('pointercancel', onRelease)
    }
  }, [])

  // "Roomy" is derived from the live width rather than stored, so the button stays
  // correct after an arbitrary grip resize.
  const roomy = box.width >= (COMPACT_SIZE.width + ROOMY_SIZE.width) / 2
  const swapPreset = useCallback(() => {
    setBox(current => {
      const target =
        current.width >= (COMPACT_SIZE.width + ROOMY_SIZE.width) / 2 ? COMPACT_SIZE : ROOMY_SIZE
      return fitToViewport({ ...current, width: target.width, height: target.height })
    })
  }, [])

  const dismiss = useCallback(() => {
    dismissedRef.current = { session: sessionKey }
    setPhase('hidden')
  }, [sessionKey])

  if (phase === 'hidden') return null

  const liveDot = (
    <span
      className={`inline-block w-1.5 h-1.5 rounded-full ${frame ? 'animate-pulse' : ''}`}
      style={{ backgroundColor: frame ? 'var(--ok)' : 'var(--muted)' }}
      aria-hidden
    />
  )

  if (phase === 'chip') {
    return (
      <button
        className="fixed z-[60] bottom-4 left-4 flex items-center gap-2 px-3 py-2 rounded-full border border-border bg-card shadow-lg hover:bg-bg-hover transition-colors"
        onClick={() => setPhase('open')}
        aria-label={i18nT('components.computerUseLiveView.show_live_desktop_view')}
        title={i18nT('components.computerUseLiveView.show_live_desktop_view')}
      >
        <AppWindow className="lucide-inline text-muted" />
        <span className="text-[12px] font-medium text-text">{i18nT('components.computerUseLiveView.desktop')}</span>
        {liveDot}
      </button>
    )
  }

  const headerLabel = appName
    ? i18nT('components.computerUseLiveView.desktop_app', { name: appName })
    : i18nT('components.computerUseLiveView.desktop_live')

  return (
    <div
      className="fixed z-[60] flex flex-col rounded-xl border border-border bg-card shadow-xl overflow-hidden"
      style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
      role="dialog"
      aria-label={i18nT('components.computerUseLiveView.live_desktop_view')}
    >
      {GRIP_ORDER.map(grip => (
        <div
          key={grip}
          role="separator"
          aria-label={i18nT(GRIP_LABEL_KEY[grip])}
          title={i18nT('components.computerUseLiveView.drag_to_resize')}
          onPointerDown={beginGesture(grip)}
          className={`absolute ${GRIP_STYLES[grip].box} ${GRIP_STYLES[grip].cursor} ${
            GRIP_STYLES[grip].onTop ? 'z-20' : 'z-10'
          }`}
        />
      ))}

      <header
        className="flex items-center gap-2 px-3 py-2 border-b border-border cursor-move select-none"
        style={{ backgroundColor: 'var(--bg-elevated)' }}
        onPointerDown={beginGesture(null)}
      >
        <AppWindow className="lucide-inline shrink-0 text-muted" />
        <span className="shrink-0 max-w-[220px] truncate text-[13px] font-medium text-text" title={headerLabel}>
          {headerLabel}
        </span>
        {liveDot}
        {sessionName ? (
          <span className="flex-1 min-w-0 truncate text-[12px] text-muted" title={sessionName}>
            · {sessionName}
          </span>
        ) : (
          <div className="flex-1" />
        )}
        <button
          onPointerDown={event => event.stopPropagation()}
          onClick={swapPreset}
          aria-label={roomy ? i18nT('components.computerUseLiveView.shrink_live_desktop_view') : i18nT('components.computerUseLiveView.enlarge_live_desktop_view')}
          title={roomy ? i18nT('components.computerUseLiveView.shrink') : i18nT('components.computerUseLiveView.enlarge')}
          className="relative z-30 p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
        >
          {roomy ? <Minimize2 className="lucide-inline" /> : <Maximize2 className="lucide-inline" />}
        </button>
        <button
          onPointerDown={event => event.stopPropagation()}
          onClick={() => setPhase('chip')}
          aria-label={i18nT('components.computerUseLiveView.minimize_live_desktop_view_to_corner')}
          title={i18nT('components.computerUseLiveView.minimize_to_corner')}
          className="relative z-30 p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
        >
          <Minus className="lucide-inline" />
        </button>
        <button
          onPointerDown={event => event.stopPropagation()}
          onClick={dismiss}
          aria-label={i18nT('components.computerUseLiveView.close_live_desktop_view')}
          title={i18nT('components.computerUseLiveView.close')}
          className="relative z-30 p-1 rounded hover:bg-bg-hover text-muted hover:text-text transition-colors"
        >
          <X className="lucide-inline" />
        </button>
      </header>

      <div className="relative bg-black flex-1 min-h-0 flex items-center justify-center">
        {frame ? (
          <img
            src={frame}
            alt={i18nT('components.computerUseLiveView.live_desktop_view')}
            className="max-w-full max-h-full object-contain"
          />
        ) : (
          <div className="flex flex-col items-center gap-2 px-4 py-8 text-center text-muted">
            <AppWindow className="lucide-inline" />
            <span className="text-[11px]">
              {i18nT('components.computerUseLiveView.no_desktop_frames_yet_frames_appear_when_the_age')}
            </span>
          </div>
        )}
      </div>

      <footer className="px-3 py-1.5 border-t border-border text-[11px] text-muted flex items-center justify-between gap-2">
        <span className="truncate">
          {box.width > 420
            ? i18nT('components.computerUseLiveView.read_only_relayed_from_the_agent_s_own_screensho')
            : i18nT('components.computerUseLiveView.read_only')}
        </span>
        {lastTs && (
          <span className="shrink-0">{i18nT('components.computerUseLiveView.updated')} {fmtTimeNumeric(lastTs)}</span>
        )}
      </footer>
    </div>
  )
}
