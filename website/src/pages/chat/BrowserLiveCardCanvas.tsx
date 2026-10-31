import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'

import { browserLiveStore } from '../../hooks/useBrowserLive'
import { createImeLatch, type ImeLatch } from '../../hooks/useImeGuard'
import type { DecodedBrowserFrame } from '../../hooks/useBrowserLive.frame'
import {
  desktopProxyKeyIsForwarded, diffToEvents, focusOfReply, isHandBackKey, isImePlaceholderKey, isSwipe, keyboardHints,
  keyToEvent, keyUpFor, MULTI_CLICK_PX, nextClickCount, pointerToEvent, proxyKeyIsForwarded, swipeToWheel, TEXT_CHUNK,
  wheelToEvent, type BrowserInputEvent, type FrameSize, type PressRecord, type Pt,
} from '../../hooks/useBrowserLive.input'

type CoalescedKind = 'move' | 'wheel'
/**
 * The one open coalescing job on the input chain. It holds the latest move and
 * the wheel deltas summed so far, and when its turn comes posts both, in the
 * order each kind FIRST arrived (the wheel carries its own position, so the
 * pointer still ends where the person scrolled). Any discrete event closes it.
 */
interface CoalesceJob { first: CoalescedKind | null; move: BrowserInputEvent | null; wheel: BrowserInputEvent | null }

/** A key pressed for the page: its first down, whether any down went out, and downs still queued. */
interface HeldKey { down: BrowserInputEvent; sent: boolean; pending: number }

/** A mouse press the page may hold. */
interface HeldPress { button: number; count: number; down: BrowserInputEvent; last: Pt; sent: boolean }
/** What a queued job reports back: when its turn came, and whether it reached the page. */
interface JobHooks { start?: () => void; done?: (sent: boolean) => void }

/** Wheel deltas add up, at the newest position. */
function addWheel(prev: BrowserInputEvent, next: BrowserInputEvent): BrowserInputEvent {
  return { ...next, dx: (prev.dx ?? 0) + (next.dx ?? 0), dy: (prev.dy ?? 0) + (next.dy ?? 0) }
}

/**
 * What the proxy holds between keystrokes: one zero-width space, so a phone
 * keyboard's Backspace always has something to delete and so always reports it
 * (Android sends no `beforeinput` for a delete in an empty field).
 */
const PROXY_FILL = '\u200b'

/** iOS reopens no keyboard for a focus outside a tap, so the proxy is not refocused there. */
function isIOS(): boolean {
  if (typeof navigator === 'undefined') return false
  return /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
}

/** A touch-first device, whose keyboard the proxy raises only from a tap. */
function isCoarsePointer(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches
}

/**
 * The picture in the live-browser card: a `<canvas>` painted from the frame
 * ring OUTSIDE React's render cycle, so 60 frames a second cost no React work.
 *
 * Painting. Each frame is decoded with `createImageBitmap` and drawn; while a
 * decode is in flight the newest arrival waits and every frame between is
 * dropped, so a slow machine shows the latest picture rather than falling
 * behind. The password-field boxes the relay reports are painted over AFTER
 * the frame, in the card's own background colour, so a password never shows
 * even for one frame. Nothing paints while the card is off screen: the owner
 * tells this component so through `visible`.
 *
 * Takeover. While `interactive`, pointer, wheel and key events on the canvas
 * are mapped to frame pixels (`useBrowserLive.input`) and posted to the
 * gateway, all on one ordered chain; moves and wheels are coalesced on it, so
 * a drag never overtakes its press. Clicks are counted here (double, triple).
 * Typing goes through a hidden proxy field on desktop and phone alike, so an
 * IME can compose into it; on a desktop every other key still goes to the
 * page as a real key. Esc hands the browser back (unless an IME is using it)
 * and never reaches the page. Keystrokes are posted and
 * forgotten: nothing here logs, stores or renders what was typed.
 *
 * Replay. `replay(frames)` repaints a recorded sequence with its original
 * spacing (gaps capped so a long pause does not stall the replay); a live
 * frame or `stop()` ends it.
 */

export interface BrowserLiveCanvasHandle {
  /** Replay a recorded frame sequence; resolves when it ends or is stopped. */
  replay: (frames: readonly DecodedBrowserFrame[]) => Promise<void>
  /** Stop a replay and show the newest live frame again. */
  stop: () => void
  /** Draw one frame now (the Done row's "Last frame"). */
  show: (frame: DecodedBrowserFrame) => void
  /** The size of the last frame drawn, or null before the first. */
  frameSize: () => FrameSize | null
  focus: () => void
}

interface Props {
  sessionKey: string
  turnId: string
  /** Paint only while true; an off-screen card decodes nothing. */
  visible: boolean
  /** Route input to the browser (the person is in control). */
  interactive: boolean
  /** Esc, or any other hand-back gesture the canvas owns. */
  onHandBack?: () => void
  /** Hover-to-take-over affordance: a click while live claims input. */
  onClaim?: () => void
  /** Told when the drawn frame's pixel size changes, so the box can take its ratio. */
  onFrameSize?: (size: FrameSize) => void
  className?: string
  'aria-label': string
}

/** The longest gap a replay keeps between two frames. */
const REPLAY_MAX_GAP_MS = 500
const REPLAY_MIN_GAP_MS = 16

/** A frame's JPEG as something a 2D context can draw (also the replay's in-memory frames). */
export async function decode(frame: DecodedBrowserFrame): Promise<ImageBitmap | HTMLImageElement> {
  const blob = new Blob([frame.jpeg as BlobPart], { type: 'image/jpeg' })
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob)
  // A host without createImageBitmap (an old shim, the test DOM): the Image route.
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob)
    const img = new Image()
    img.onload = () => { URL.revokeObjectURL(url); resolve(img) }
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('decode')) }
    // An image, not a script: url is a blob: URL made from the frame bytes above.
    // nosemgrep: semgrep.kirocrew.frontend-external-script-inject
    img.src = url
  })
}

const BrowserLiveCardCanvas = forwardRef<BrowserLiveCanvasHandle, Props>(function BrowserLiveCardCanvas(
  { sessionKey, turnId, visible, interactive, onHandBack, onClaim, onFrameSize, className, 'aria-label': ariaLabel },
  ref,
) {
  const onFrameSizeRef = useRef(onFrameSize)
  onFrameSizeRef.current = onFrameSize
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const sizeRef = useRef<FrameSize | null>(null)
  const latestRef = useRef<DecodedBrowserFrame | null>(null)
  const pendingRef = useRef<DecodedBrowserFrame | null>(null)
  const decodingRef = useRef(false)
  const replayingRef = useRef(false)
  const replayTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const replayDone = useRef<(() => void) | null>(null)
  const visibleRef = useRef(visible)
  visibleRef.current = visible

  const maskColor = useCallback((): string => {
    const c = canvasRef.current
    if (!c) return '#000'
    const v = getComputedStyle(c).getPropertyValue('--card').trim()
    return v || '#000'
  }, [])

  const draw = useCallback(async (frame: DecodedBrowserFrame) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const { w, h, mask } = frame.header
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w
      canvas.height = h
    }
    const prev = sizeRef.current
    sizeRef.current = { w, h }
    if (!prev || prev.w !== w || prev.h !== h) onFrameSizeRef.current?.({ w, h })
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    let img: ImageBitmap | HTMLImageElement
    try { img = await decode(frame) } catch { return }
    ctx.drawImage(img, 0, 0, w, h)
    if ('close' in img) img.close()
    if (mask.length) {
      ctx.fillStyle = maskColor()
      for (const [x, y, rw, rh] of mask) ctx.fillRect(x, y, rw, rh)
    }
  }, [maskColor])

  /** Paint the newest waiting frame; one decode in flight at a time. */
  const pump = useCallback(async () => {
    if (decodingRef.current) return
    decodingRef.current = true
    try {
      while (pendingRef.current) {
        const next = pendingRef.current
        pendingRef.current = null
        await draw(next)
      }
    } finally {
      decodingRef.current = false
    }
  }, [draw])

  const onLiveFrame = useCallback((frame: DecodedBrowserFrame) => {
    latestRef.current = frame
    if (replayingRef.current || !visibleRef.current) return
    pendingRef.current = frame
    void pump()
  }, [pump])

  useEffect(() => browserLiveStore.subscribeFrames(sessionKey, turnId, onLiveFrame), [sessionKey, turnId, onLiveFrame])

  // Coming back on screen paints the frame that arrived while hidden.
  useEffect(() => {
    if (visible && latestRef.current && !replayingRef.current) {
      pendingRef.current = latestRef.current
      void pump()
    }
  }, [visible, pump])

  const stopReplay = useCallback(() => {
    if (replayTimer.current) { clearTimeout(replayTimer.current); replayTimer.current = null }
    replayingRef.current = false
    replayDone.current?.()
    replayDone.current = null
    if (latestRef.current) { pendingRef.current = latestRef.current; void pump() }
  }, [pump])

  useImperativeHandle(ref, () => ({
    replay: (frames) => new Promise<void>((resolve) => {
      stopReplay()
      if (frames.length === 0) { resolve(); return }
      replayingRef.current = true
      replayDone.current = resolve
      let i = 0
      const step = () => {
        const f = frames[i]
        pendingRef.current = f
        void pump()
        i += 1
        if (i >= frames.length) {
          replayTimer.current = setTimeout(() => stopReplay(), REPLAY_MAX_GAP_MS)
          return
        }
        const gap = Math.min(REPLAY_MAX_GAP_MS, Math.max(REPLAY_MIN_GAP_MS, frames[i].receivedAt - f.receivedAt))
        replayTimer.current = setTimeout(step, gap)
      }
      step()
    }),
    stop: stopReplay,
    show: (frame) => { stopReplay(); pendingRef.current = frame; void pump() },
    frameSize: () => sizeRef.current,
    // No scroll on focus: the card places itself (see `browserLiveClearance`).
    focus: () => canvasRef.current?.focus({ preventScroll: true }),
  }), [pump, stopReplay])

  useEffect(() => () => { if (replayTimer.current) clearTimeout(replayTimer.current) }, [])

  // ---- takeover input -------------------------------------------------------

  const moveRef = useRef<BrowserInputEvent | null>(null)
  const moveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** A swipe's wheel steps, summed until its 16 ms tick. */
  const wheelRef = useRef<BrowserInputEvent | null>(null)
  const wheelTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Every input goes out on ONE chain, one POST at a time: two in flight at
  // once may reach the page in either order ("ab" typed as "ba", a release
  // before its press, a drag's move before the press that starts it).
  // Moves and wheels are coalesced on the chain into at most ONE open job
  // (`CoalesceJob`), so a burst during a slow POST -- alternating moves and
  // wheels included -- is one job, not a growing queue. A discrete event first
  // flushes the moves and wheels still waiting on their 16 ms tick into that
  // job, then closes it, so nothing recorded before it is sent after it and
  // nothing recorded after it rides a job queued before it.
  // Every job carries the takeover generation it was queued in; a hand-back
  // bumps it, so a job still queued then never reaches the page, even after a
  // retake. Key and mouse-button releases are the exception: they only undo a
  // press the page already has (each checks that its press went out), and
  // dropping one would leave the key or button held there.
  const interactiveRef = useRef(interactive)
  interactiveRef.current = interactive
  const genRef = useRef(0)
  const ordered = useRef<Promise<unknown>>(Promise.resolve())
  /** Jobs on the chain not yet finished: a hand-back waits only when there are some. */
  const inFlight = useRef(0)
  const openJob = useRef<CoalesceJob | null>(null)

  // `hooks.start` runs when the job's turn comes; `hooks.done` is told, once
  // the job has run, whether every event of it reached the page: it passed the
  // generation check AND the gateway answered that it dispatched it. A press
  // records that, so its release is sent only for a press the page actually has.
  const enqueue = useCallback((take: () => readonly BrowserInputEvent[], release = false, hooks?: JobHooks): Promise<unknown> => {
    const gen = genRef.current
    inFlight.current += 1
    // A failed send must not stop every later one: the chain always resolves.
    const next = ordered.current
      .then(async () => {
        let sent = false
        try {
          hooks?.start?.()
          const evs = take()
          // A release undoes a press of its own takeover, so it may run after the
          // hand-back; once the card has taken control again it is dropped, so it can
          // never lift a key the new takeover is holding.
          const current = gen === genRef.current
          const allowed = release ? current || !interactiveRef.current : current && interactiveRef.current
          if (!allowed) return undefined
          let reply: unknown
          sent = evs.length > 0
          for (const ev of evs) {
            const out = await browserLiveStore.sendInput(sessionKey, ev)
            sent = sent && out.sent
            reply = out.reply
          }
          return reply
        } finally {
          hooks?.done?.(sent)
        }
      })
      .catch(() => undefined)
      .finally(() => { inFlight.current -= 1 })
    ordered.current = next
    return next
  }, [sessionKey])

  /** Put a move or wheel in the open coalescing job (opening one on the chain if none is). */
  const coalesce = useCallback((kind: CoalescedKind, ev: BrowserInputEvent) => {
    let job = openJob.current
    if (!job) {
      const j: CoalesceJob = { first: null, move: null, wheel: null }
      job = j
      openJob.current = j
      void enqueue(() => {
        if (openJob.current === j) openJob.current = null
        const out = j.first === 'wheel' ? [j.wheel, j.move] : [j.move, j.wheel]
        return out.filter((e): e is BrowserInputEvent => e !== null)
      })
    }
    if (!job.first) job.first = kind
    if (kind === 'move') job.move = ev
    else job.wheel = job.wheel ? addWheel(job.wheel, ev) : ev
  }, [enqueue])

  const flushMove = useCallback(() => {
    if (moveTimer.current) { clearTimeout(moveTimer.current); moveTimer.current = null }
    const m = moveRef.current
    moveRef.current = null
    if (m) coalesce('move', m)
  }, [coalesce])

  const flushWheel = useCallback(() => {
    if (wheelTimer.current) { clearTimeout(wheelTimer.current); wheelTimer.current = null }
    const w = wheelRef.current
    wheelRef.current = null
    // On the chain, so a tap right after a swipe lands on the scrolled page.
    if (w) coalesce('wheel', w)
  }, [coalesce])

  /** A discrete job: the moves and wheels recorded before it go first. */
  const sendJobInOrder = useCallback((take: () => readonly BrowserInputEvent[], hooks?: JobHooks): Promise<unknown> => {
    flushMove()
    flushWheel()
    openJob.current = null
    return enqueue(take, false, hooks)
  }, [enqueue, flushMove, flushWheel])

  const sendInOrder = useCallback((ev: BrowserInputEvent, hooks?: JobHooks): Promise<unknown> =>
    sendJobInOrder(() => [ev], hooks), [sendJobInOrder])

  /** The last desktop press, so the next one can tell a double click; null once it cannot be one. */
  const lastPress = useRef<PressRecord | null>(null)
  /** The press the next release belongs to: its button, count and down, where
   *  the pointer last was, and whether the down went out (set on the chain). */
  const heldPress = useRef<HeldPress | null>(null)
  /** Keys pressed for the page (proxy or canvas), by code. */
  const keysDown = useRef(new Map<string, HeldKey>())

  /** Queue a key down, recording it so a release (or repeat) knows whether it went out. */
  const sendKeyDown = useCallback((code: string, down: BrowserInputEvent) => {
    let held = keysDown.current.get(code)
    if (!held) { held = { down, sent: false, pending: 0 }; keysDown.current.set(code, held) }
    const rec = held
    rec.pending += 1
    void sendInOrder(down, { start: () => { rec.pending -= 1 }, done: (sent) => { if (sent) rec.sent = true } })
  }, [sendInOrder])

  /** A repeat with an earlier down of its key still queued adds nothing but queue. */
  const isQueuedRepeat = (e: React.KeyboardEvent) => e.repeat && (keysDown.current.get(e.code || e.key)?.pending ?? 0) > 0

  /** A key's release, in order, sent only when one of its downs went out. */
  const sendKeyUp = useCallback((code: string, up: BrowserInputEvent): boolean => {
    const rec = keysDown.current.get(code)
    if (!rec) return false
    keysDown.current.delete(code)
    void sendJobInOrder(() => (rec.sent ? [up] : []))
    return true
  }, [sendJobInOrder])

  /**
   * Release every key, and the mouse button, the page holds, then forget them.
   * Generation-exempt: each only undoes a press the page already has, and
   * checks on the chain that its press went out, so no release is sent for a
   * press that was dropped.
   */
  const releaseKeys = useCallback(() => {
    for (const rec of keysDown.current.values()) void enqueue(() => (rec.sent ? [keyUpFor(rec.down)] : []), true)
    keysDown.current.clear()
  }, [enqueue])

  const releaseMouse = useCallback(() => {
    const press = heldPress.current
    heldPress.current = null
    if (!press) return
    void enqueue(() => (press.sent ? [{ ...press.down, action: 'up', x: press.last.x, y: press.last.y }] : []), true)
  }, [enqueue])

  // A hand-back from anywhere (the card's button, the stage, Esc) first queues
  // the releases for what this canvas holds and waits for its chain to send
  // them, so they reach the page before the gateway opens the lock.
  useEffect(() => browserLiveStore.onHandBack(sessionKey, () => {
    releaseKeys()
    releaseMouse()
    return inFlight.current > 0 ? ordered.current : null
  }), [sessionKey, releaseKeys, releaseMouse])

  const box = () => {
    const r = canvasRef.current?.getBoundingClientRect()
    return r ? { left: r.left, top: r.top, width: r.width, height: r.height } : { left: 0, top: 0, width: 0, height: 0 }
  }

  // ---- touch typing: the proxy textarea --------------------------------------
  // A tap focuses the proxy in the touchend handler itself: iOS opens the
  // keyboard only for a focus made synchronously inside a touchend or click,
  // never after an await. The tap's reply then says what took focus in the
  // page: a field keeps the keyboard, with the field's keyboard type; anything
  // else closes it again.
  const proxyRef = useRef<HTMLInputElement>(null)
  /** A desktop keyboard types (a mouse press or keyboard focus aimed the proxy),
   *  not a phone's: every real key goes to the page as a key. A tap clears it. */
  const desktopRef = useRef(false)
  /** An IME composition is live in the proxy. */
  const composingRef = useRef(false)
  /** The shared IME latch: WebKit's candidate-commit Enter arrives after
   *  `compositionend` with `isComposing` false, so only the latch can keep it
   *  from reaching the page as a real Enter. */
  const imeRef = useRef<ImeLatch | null>(null)
  /** The proxy's value as the page last heard it; the next edit is diffed against it. */
  const proxySent = useRef(PROXY_FILL)
  const tapSeq = useRef(0)

  const resetProxy = () => {
    const p = proxyRef.current
    if (!p) return
    p.value = PROXY_FILL
    proxySent.current = PROXY_FILL
    try { p.setSelectionRange(PROXY_FILL.length, PROXY_FILL.length) } catch { /* not focused */ }
  }

  /** Raise the keyboard. Runs inside the tap's own handler; must not await. */
  const focusProxyAt = (clientX: number, clientY: number) => {
    const p = proxyRef.current
    if (!p) return
    desktopRef.current = false
    // Inside the viewport and at the tap, so no browser scrolls or refuses it.
    p.style.left = `${Math.max(0, Math.min(window.innerWidth - 2, Math.round(clientX)))}px`
    p.style.top = `${Math.max(0, Math.min(window.innerHeight - 2, Math.round(clientY)))}px`
    resetProxy()
    // Start every tap with the text keyboard; the reply refines it. iOS keeps
    // the keyboard it opened with, so the default is the one that types.
    p.type = 'text'
    p.inputMode = 'text'
    // Android hides the keyboard (Back) without a blur, and a focus() on what
    // is already focused does nothing; blur first so it opens again. iOS blurs
    // when its keyboard closes, so a focused proxy there has its keyboard up.
    if (document.activeElement === p && !isIOS()) p.blur()
    p.focus({ preventScroll: true })
  }

  /**
   * A desktop keyboard's target. At the pointer, so an IME's candidate window
   * opens near where the person is typing; none of the phone keyboard's
   * type and refocus handling, and a focused proxy keeps any word being
   * composed.
   */
  const focusProxyForDesktop = (clientX: number, clientY: number) => {
    const p = proxyRef.current
    if (!p) return
    p.style.left = `${Math.max(0, Math.min(window.innerWidth - 2, Math.round(clientX)))}px`
    p.style.top = `${Math.max(0, Math.min(window.innerHeight - 2, Math.round(clientY)))}px`
    desktopRef.current = true
    if (document.activeElement !== p) p.focus({ preventScroll: true })
  }

  /** The canvas took keyboard focus (Tab, or the card's own `focus()`): the proxy types. */
  const onCanvasFocus = () => {
    // A phone's keyboard opens only for a tap (see `tap`), never for a focus.
    if (!interactive || isCoarsePointer()) return
    const r = canvasRef.current?.getBoundingClientRect()
    focusProxyForDesktop(r ? r.left + r.width / 2 : 0, r ? r.top + r.height / 2 : 0)
  }
  const [proxyFocused, setProxyFocused] = useState(false)

  const afterTap = (reply: Promise<unknown>) => {
    const p = proxyRef.current
    if (!p) return
    const seq = ++tapSeq.current
    const gen = genRef.current
    void reply.then((r) => {
      // A reply from before a hand-back belongs to a takeover that has ended.
      if (seq !== tapSeq.current || gen !== genRef.current || document.activeElement !== p) return
      const focus = focusOfReply(r)
      if (!focus?.editable) { p.blur(); return }
      const { inputMode, enterKeyHint } = keyboardHints(focus)
      // A password field gets a password box: no keyboard learns, suggests
      // or composes what is typed into it.
      const type = focus.kind === 'password' ? 'password' : 'text'
      const changed = p.inputMode !== inputMode || p.enterKeyHint !== enterKeyHint || p.type !== type
      p.inputMode = inputMode
      p.enterKeyHint = enterKeyHint
      p.type = type
      // Android takes the new keyboard type on a refocus (the tap's activation
      // still holds); iOS would close the keyboard, so it keeps the text one.
      if (changed && !isIOS()) { p.blur(); p.focus({ preventScroll: true }); resetProxy() }
    })
  }

  /**
   * A desktop click's reply: a page password field gets a password proxy (no
   * IME learns or shows what is typed), anything else a text one. The proxy
   * keeps focus whatever was clicked: a desktop keyboard still types into the
   * page (shortcuts, games) when no field has focus there.
   */
  const afterDesktopClick = (reply: Promise<unknown>) => {
    const seq = ++tapSeq.current
    const gen = genRef.current
    void reply.then((r) => {
      const p = proxyRef.current
      const focus = focusOfReply(r)
      if (!p || !focus || seq !== tapSeq.current || gen !== genRef.current || !desktopRef.current) return
      const type = focus.editable && focus.kind === 'password' ? 'password' : 'text'
      if (p.type !== type) { p.type = type; resetProxy() }
    })
  }

  /** A tap on the page: one click there, in order, and the keyboard raised now. */
  const tap = (clientX: number, clientY: number) => {
    const size = sizeRef.current
    if (!size) return
    const b = box()
    const down = pointerToEvent('down', { clientX, clientY, button: 0, buttons: 1, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false }, b, size)
    if (!down) return
    focusProxyAt(clientX, clientY)
    void sendInOrder(down)
    const up: BrowserInputEvent = { ...down, action: 'up' }
    afterTap(sendInOrder(up))
  }

  // The listeners below outlive a render (re-binding would drop a swipe mid-way).
  const tapRef = useRef(tap)
  tapRef.current = tap

  // Touch is read from touch events, not pointer events: touchend is where iOS
  // honours a focus, and its preventDefault is what stops the phone's own
  // emulated mouse click -- which would focus the canvas and take the
  // keyboard away again.
  const gesture = useRef<{ id: number; start: Pt; last: Pt; swiping: boolean } | null>(null)

  useEffect(() => {
    const c = canvasRef.current
    if (!c || !interactive) return
    const find = (list: TouchList, id: number): Touch | null => {
      for (let i = 0; i < list.length; i += 1) if (list[i].identifier === id) return list[i]
      return null
    }
    const onTouchStart = (e: TouchEvent) => {
      // A second finger (pinch) ends the gesture: it is neither a tap nor a swipe.
      if (e.touches.length !== 1) { gesture.current = null; return }
      const t = e.changedTouches[0]
      const at = { x: t.clientX, y: t.clientY }
      gesture.current = { id: t.identifier, start: at, last: at, swiping: false }
    }
    const onTouchMove = (e: TouchEvent) => {
      // The chat behind the card must not scroll instead of the page.
      if (e.cancelable) e.preventDefault()
      const g = gesture.current
      if (!g) return
      const t = find(e.changedTouches, g.id)
      if (!t) return
      const now = { x: t.clientX, y: t.clientY }
      if (!g.swiping && !isSwipe(g.start, now)) return
      g.swiping = true
      const size = sizeRef.current
      const step = size ? swipeToWheel(g.last, now, g.start, box(), size) : null
      g.last = now
      if (!step) return
      const w = wheelRef.current
      wheelRef.current = w ? { ...w, dx: (w.dx ?? 0) + (step.dx ?? 0), dy: (w.dy ?? 0) + (step.dy ?? 0) } : step
      if (!wheelTimer.current) wheelTimer.current = setTimeout(flushWheel, 16)
    }
    const onTouchEnd = (e: TouchEvent) => {
      const g = gesture.current
      if (!g) return
      const t = find(e.changedTouches, g.id)
      if (!t) return
      gesture.current = null
      if (e.cancelable) e.preventDefault()
      if (g.swiping) { flushWheel(); return }
      tapRef.current(t.clientX, t.clientY)
    }
    const onTouchCancel = () => { gesture.current = null; flushWheel() }
    c.addEventListener('touchstart', onTouchStart, { passive: true })
    c.addEventListener('touchmove', onTouchMove, { passive: false })
    c.addEventListener('touchend', onTouchEnd, { passive: false })
    c.addEventListener('touchcancel', onTouchCancel)
    return () => {
      c.removeEventListener('touchstart', onTouchStart)
      c.removeEventListener('touchmove', onTouchMove)
      c.removeEventListener('touchend', onTouchEnd)
      c.removeEventListener('touchcancel', onTouchCancel)
      gesture.current = null
    }
  }, [interactive, flushWheel])

  const isTouch = (e: React.PointerEvent) => e.pointerType === 'touch' || e.pointerType === 'pen'

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive || isTouch(e)) return
    e.preventDefault()
    // Typing goes through the proxy on a desktop too: a canvas takes no IME
    // composition, so Chinese, Japanese or Korean input could not be typed.
    focusProxyForDesktop(e.clientX, e.clientY)
    try { canvasRef.current?.setPointerCapture(e.pointerId) } catch { /* not a pointer device */ }
    flushMove()
    // A pointer event's `detail` is always 0: the card counts clicks itself.
    const now = Date.now()
    const clicks = nextClickCount(lastPress.current, now, e.clientX, e.clientY, e.button)
    const size = sizeRef.current
    const down = size ? pointerToEvent('down', e.nativeEvent, box(), size, clicks) : null
    // A press in the letterbox is no click on the page, so it counts for nothing.
    if (!down) return
    lastPress.current = { t: now, x: e.clientX, y: e.clientY, button: e.button, count: clicks }
    const press: HeldPress = { button: e.button, count: clicks, down, last: { x: down.x ?? 0, y: down.y ?? 0 }, sent: false }
    heldPress.current = press
    void sendInOrder(down, { done: (sent) => { press.sent = sent } })
  }
  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive || isTouch(e)) return
    e.preventDefault()
    try { canvasRef.current?.releasePointerCapture(e.pointerId) } catch { /* not captured */ }
    flushMove()
    // A release belongs to a press the page was sent: a press in the
    // letterbox, or one dropped by a hand-back, has no release and no reply.
    const press = heldPress.current
    if (!press || press.button !== e.button) return
    heldPress.current = null
    const size = sizeRef.current
    // The release carries its press's count.
    const up = size ? pointerToEvent('up', e.nativeEvent, box(), size, press.count) : null
    if (up) afterDesktopClick(sendJobInOrder(() => (press.sent ? [up] : [])))
  }
  /** A cancelled pointer (the OS took it) ends any multi-click, and lets go of the page's button. */
  const onPointerCancel = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (isTouch(e)) return
    lastPress.current = null
    flushMove()
    releaseMouse()
  }

  /** Hand the browser back from the proxy: release what is held first (the blur does). */
  const handBackFromProxy = () => {
    proxyRef.current?.blur()
    releaseKeys()
    onHandBack?.()
  }

  // The proxy's keys. On a desktop every real key goes to the page as a key
  // (shortcuts, games and key repeat need the key itself) and the proxy is
  // left untouched; only an IME's composition edits it, and the edit is sent
  // as text (see `syncProxy`). On a phone, named keys go as keys and everything
  // else edits the proxy. All of it stops here, so nothing typed for the page
  // reaches a dashboard shortcut.
  const onProxyKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    e.stopPropagation()
    if (!interactive) return
    const ime = imeRef.current
    if (isHandBackKey(e.key)) {
      // Esc during a composition cancels the candidate; it is the IME's.
      if (composingRef.current || (ime && !ime.claimSyntheticKey(e))) return
      e.preventDefault()
      handBackFromProxy()
      return
    }
    const desktop = desktopRef.current
    if (desktop ? composingRef.current || !desktopProxyKeyIsForwarded(e.nativeEvent) : !proxyKeyIsForwarded(e.nativeEvent)) return
    // The post-composition latch is for the candidate-commit Enter alone on a
    // desktop: a live composition is already filtered above, and the letter
    // typed right after a commit is the person's next keystroke.
    if (ime && (!desktop || e.key === 'Enter') && !ime.claimSyntheticKey(e)) return
    e.preventDefault()
    // Repeats behind a stalled POST would only grow the queue.
    if (desktop && isQueuedRepeat(e)) return
    sendKeyDown(e.code || e.key, keyToEvent('down', e.nativeEvent))
    // On a phone, Enter, Tab and the arrows move the page's caret away from
    // what the proxy mirrors; start the mirror afresh.
    if (!desktop) resetProxy()
  }
  const onProxyKeyUp = (e: React.KeyboardEvent<HTMLInputElement>) => {
    e.stopPropagation()
    if (!sendKeyUp(e.code || e.key, keyToEvent('up', e.nativeEvent))) return
    e.preventDefault()
  }

  useEffect(() => {
    const p = proxyRef.current
    if (!p) return
    if (!interactive) { if (document.activeElement === p) p.blur(); resetProxy(); return }
    composingRef.current = false
    const ime = createImeLatch()
    imeRef.current = ime
    let commitTimer: ReturnType<typeof setTimeout> | null = null
    /** Send what changed since the page last heard; refill once the IME lets go. */
    const syncProxy = () => {
      const next = p.value
      for (const ev of diffToEvents(proxySent.current, next)) void sendInOrder(ev)
      proxySent.current = next
      // Forget the text once its word is done: back to the one character there
      // is to delete (so Backspace in an "empty" field still reports). The word
      // being typed stays, so autocorrect or a suggestion can replace it (the
      // diff takes the old word back out). Never mid-composition: the IME owns
      // the word until it commits. A password is forgotten at once.
      const wordDone = p.type === 'password' || /\s$/.test(next) || Array.from(next).length > TEXT_CHUNK || !next.startsWith(PROXY_FILL)
      if (!composingRef.current && wordDone) resetProxy()
    }
    const onInput = () => syncProxy()
    const onCompositionEnd = (e: CompositionEvent) => {
      composingRef.current = false
      ime.onCompositionEnd(e.data)
      // Safari sends its last input after compositionend, Chrome before: sync once
      // it has. A committed word is final (the IME corrects inside the
      // composition), so the proxy forgets it.
      if (commitTimer) clearTimeout(commitTimer)
      commitTimer = setTimeout(() => {
        commitTimer = null
        syncProxy()
        if (!composingRef.current) resetProxy()
      }, 0)
    }
    // A blur with keys still down (the keyboard closed mid-press, focus left
    // the card) sends the page a key-up for each, and the proxy lets go of any
    // word: the page already has it (every edit was sent as it happened).
    const onBlur = () => {
      releaseKeys()
      composingRef.current = false
      ime.reset()
      if (commitTimer) { clearTimeout(commitTimer); commitTimer = null }
      resetProxy()
    }
    const onCompositionStart = () => { composingRef.current = true; ime.onCompositionStart() }
    p.addEventListener('input', onInput)
    p.addEventListener('compositionstart', onCompositionStart)
    p.addEventListener('compositionend', onCompositionEnd)
    p.addEventListener('blur', onBlur)
    return () => {
      p.removeEventListener('input', onInput)
      p.removeEventListener('compositionstart', onCompositionStart)
      p.removeEventListener('compositionend', onCompositionEnd)
      p.removeEventListener('blur', onBlur)
      if (commitTimer) clearTimeout(commitTimer)
      ime.reset()
      if (imeRef.current === ime) imeRef.current = null
      composingRef.current = false
      resetProxy()
    }
  }, [interactive, sendInOrder, releaseKeys])

  // Handing back ends this takeover: what it holds is released, what it still
  // has queued or waiting on a tick is dropped (the next takeover is a new
  // generation), and no click of it counts toward the next one's.
  useEffect(() => {
    if (interactive) return
    releaseKeys()
    releaseMouse()
    genRef.current += 1
    // A click reply still in flight belongs to the takeover that just ended.
    tapSeq.current += 1
    openJob.current = null
    if (moveTimer.current) { clearTimeout(moveTimer.current); moveTimer.current = null }
    if (wheelTimer.current) { clearTimeout(wheelTimer.current); wheelTimer.current = null }
    moveRef.current = null
    wheelRef.current = null
    lastPress.current = null
    desktopRef.current = false
  }, [interactive, releaseKeys, releaseMouse])
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!interactive || isTouch(e)) return
    // A drag is no click: the next press after it starts a new count.
    const press = lastPress.current
    if (press && e.buttons && Math.hypot(e.clientX - press.x, e.clientY - press.y) > MULTI_CLICK_PX) lastPress.current = null
    const size = sizeRef.current
    if (!size) return
    const move = pointerToEvent('move', e.nativeEvent, box(), size)
    moveRef.current = move
    // Where a cancelled or handed-back press is released.
    const held = heldPress.current
    if (held && move) held.last = { x: move.x ?? held.last.x, y: move.y ?? held.last.y }
    if (!moveTimer.current) moveTimer.current = setTimeout(flushMove, 16)
  }
  // A keystroke meant for the page is the page's alone: `stopPropagation` keeps
  // it from the dashboard's window-level shortcut handler (Alt+digit switches
  // session, Alt+letter navigates), which would otherwise unmount this card
  // mid-takeover -- and on macOS Option+letter is how accents are typed.
  // Typing normally reaches the proxy (the canvas hands it focus); these are
  // the fallback for a key that still lands on the canvas. An IME placeholder
  // key is never a key for the page.
  const onKeyDown = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (!interactive) return
    e.preventDefault()
    e.stopPropagation()
    if (isHandBackKey(e.key)) { onHandBack?.(); return }
    if (isImePlaceholderKey(e.nativeEvent) || isQueuedRepeat(e)) return
    // Recorded with the proxy's keys, so a hand-back or blur releases it too.
    sendKeyDown(e.code || e.key, keyToEvent('down', e.nativeEvent))
  }
  const onKeyUp = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (!interactive) return
    e.preventDefault()
    e.stopPropagation()
    if (isHandBackKey(e.key) || isImePlaceholderKey(e.nativeEvent)) return
    sendKeyUp(e.code || e.key, keyToEvent('up', e.nativeEvent))
  }

  // Wheel is registered natively: React's wheel listener is passive, so a
  // preventDefault there cannot stop the transcript from scrolling under the
  // person's hand while they scroll the page in the frame.
  useEffect(() => {
    const c = canvasRef.current
    if (!c || !interactive) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const size = sizeRef.current
      const ev = size ? wheelToEvent(e, box(), size) : null
      if (ev) coalesce('wheel', ev)
    }
    const onContextMenu = (e: MouseEvent) => e.preventDefault()
    c.addEventListener('wheel', onWheel, { passive: false })
    c.addEventListener('contextmenu', onContextMenu)
    return () => {
      c.removeEventListener('wheel', onWheel)
      c.removeEventListener('contextmenu', onContextMenu)
    }
  }, [interactive, coalesce])

  useEffect(() => () => {
    if (moveTimer.current) clearTimeout(moveTimer.current)
    if (wheelTimer.current) clearTimeout(wheelTimer.current)
  }, [])

  // Claiming: a plain click on the live frame. Keyboard users get the Take
  // over button in the header; the canvas itself is not a button while live.
  const onClick = () => { if (!interactive && onClaim) onClaim() }

  return (
    <>
    <canvas
      ref={canvasRef}
      // Taking keys and pointer input while held, a picture otherwise.
      role={interactive ? 'application' : 'img'}
      aria-label={ariaLabel}
      tabIndex={interactive ? 0 : -1}
      data-testid="browser-live-canvas"
      data-interactive={interactive ? 'true' : undefined}
      // The proxy holds the keyboard focus for the canvas, so the canvas wears its ring.
      data-proxy-focus={interactive && proxyFocused ? 'true' : undefined}
      className={`block w-full h-full object-contain bg-[var(--card)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:ring-inset data-[proxy-focus=true]:ring-2 data-[proxy-focus=true]:ring-[var(--accent)] data-[proxy-focus=true]:ring-inset select-none ${interactive ? 'cursor-default touch-none [-webkit-touch-callout:none]' : onClaim ? 'cursor-pointer' : ''} ${className ?? ''}`}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerMove={onPointerMove}
      onPointerCancel={onPointerCancel}
      onFocus={onCanvasFocus}
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      onClick={onClick}
    />
    {/* The phone keyboard's target (see focusProxyAt). In the DOM and in the
        viewport (iOS opens no keyboard for a hidden field), all but invisible,
        out of the tab order, 16px so iOS does not zoom on focus. It holds at
        most the word being typed (never in a password box). */}
    <input
      ref={proxyRef}
      type="text"
      aria-label={`${ariaLabel}: type into the page`}
      data-testid="browser-live-type-proxy"
      tabIndex={-1}
      autoCapitalize="off"
      autoCorrect="off"
      autoComplete="off"
      spellCheck={false}
      defaultValue={PROXY_FILL}
      onKeyDown={onProxyKeyDown}
      onKeyUp={onProxyKeyUp}
      onFocus={() => setProxyFocused(true)}
      onBlur={() => setProxyFocused(false)}
      style={{ position: 'fixed', left: 0, top: 0, width: 1, height: 1, padding: 0, border: 0, opacity: 0.01, fontSize: 16, pointerEvents: 'none', background: 'transparent', color: 'transparent', caretColor: 'transparent' }}
    />
    </>
  )
})

export default BrowserLiveCardCanvas
