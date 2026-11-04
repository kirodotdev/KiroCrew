/**
 * Silence watchdog for a `/api/ws` socket that stays OPEN but stops delivering.
 *
 * A browser can keep a WebSocket whose transport is gone -- a phone that changed
 * networks, or a tab resumed after the OS froze it -- without ever firing
 * `onclose`. Nothing else notices: a reconnect catch-up only runs on close, and a
 * health probe only polls while the connection is believed down. Every one-shot
 * frame is then lost until a manual reload. The gateway pushes a `dashboard`
 * status frame on every socket every 5s (`_WS_STATUS_INTERVAL` in
 * `dashboard/ws.py`), so a socket silent for longer than the window, while its
 * page is visible, is treated as dead and replaced.
 *
 * This is the ONE implementation of that rule. The dashboard's own socket
 * (`useWebSocket`) and the two app sockets (`apps/mochi/panel/panelBridge`,
 * `apps/crew-companion/sessionWatch`) all drive it, so the three cannot drift in
 * their timing constants or their hidden-time bookkeeping. The host supplies only
 * the two things that differ per socket: how to read whether the socket is
 * currently OPEN (`isOpen`), and how to replace it (`onDead`).
 *
 * Silence is measured in VISIBLE time only. Hidden pages are skipped (timers are
 * throttled there, and a suspended tab processed nothing); on the return every
 * stamp moves past the hidden interval, so silence adds up across tab switches
 * while a frame that arrived in the background counts as arriving at the return.
 * A thawed socket then gets two checks to deliver its next status frame before it
 * can be replaced -- unless its visible silence had already passed the window
 * when the page came back, in which case the first check replaces it, so a dead
 * socket cannot outlive a run of glances each shorter than that grace. Each
 * consecutive silent replacement doubles the window up to WS_SILENCE_MAX_MS, so a
 * gateway that stopped sending status on a live socket costs one reconnect per
 * window rather than one every 20s; a socket observed live for a whole window of
 * visible time since it opened resets the count.
 */

/** A socket that has delivered nothing for this long while the page is visible
 *  is treated as dead, even when its `readyState` still reads OPEN. The gateway
 *  pushes a `dashboard` status frame on every socket every 5s
 *  (`_WS_STATUS_INTERVAL` in `dashboard/ws.py`), so this is four missed ticks. */
export const WS_SILENCE_MS = 20_000
/** How often the silence check runs: the status frame's own cadence. */
export const WS_SILENCE_CHECK_MS = 5_000
/** Ceiling for the silence window after repeated silent reconnects. */
export const WS_SILENCE_MAX_MS = 300_000

/** What a host socket must supply so the watchdog can read and remedy it. */
export interface SilenceWatchdogHost {
  /** Whether the socket currently being watched is OPEN and so eligible for a
   *  silence check. A closed/connecting socket is left to its own reconnect. */
  isOpen: () => boolean
  /** Replace the socket now. Its reconnect catch-up re-reads every frame family;
   *  the watchdog's own stamps are re-seeded by the next `markOpen`. */
  onDead: () => void
  /** Injectable clock, so a test drives the window without real time. */
  now?: () => number
}

/**
 * Framework-agnostic silence watchdog. Construct one per logical socket
 * connection (the thing that survives individual socket replacements), call
 * `markOpen` whenever a fresh socket opens and `markFrame` on every delivered
 * frame, and `start()` once to arm the interval + visibility listener. `stop()`
 * detaches both. The timing state lives here, not in the host, so every socket
 * that uses it shares one contract.
 */
export class SilenceWatchdog {
  private readonly now: () => number
  /** When the current socket last delivered a frame, in visible time. */
  private lastFrameAt = 0
  /** When the current socket's silence clock started (at open), in visible time. */
  private silenceClockStartedAt = 0
  /** When the page was hidden (0 while visible). */
  private hiddenAt = 0
  /** Until when a socket thawed by a return is spared. */
  private graceUntil = 0
  /** How many silent sockets in a row the watchdog has replaced. */
  private silentReconnects = 0

  private timer: ReturnType<typeof setInterval> | null = null
  private readonly onVisibility: () => void

  constructor(private readonly host: SilenceWatchdogHost) {
    this.now = host.now ?? (() => Date.now())
    this.onVisibility = () => this.handleVisibility()
  }

  /** A fresh socket just opened: start its silence clock from now. */
  markOpen(): void {
    this.silenceClockStartedAt = this.now()
    this.lastFrameAt = this.silenceClockStartedAt
  }

  /** The watched socket delivered a frame: it is alive as of now. */
  markFrame(): void {
    this.lastFrameAt = this.now()
  }

  /** Arm the interval and the visibility listener. Idempotent. */
  start(): void {
    if (this.timer !== null) return
    this.timer = setInterval(() => this.check(), WS_SILENCE_CHECK_MS)
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.onVisibility)
    }
  }

  /** Detach the interval and the visibility listener. Idempotent. */
  stop(): void {
    if (this.timer !== null) {
      clearInterval(this.timer)
      this.timer = null
    }
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.onVisibility)
    }
  }

  private silenceWindowMs(): number {
    return Math.min(WS_SILENCE_MS * 2 ** this.silentReconnects, WS_SILENCE_MAX_MS)
  }

  private check(): void {
    if (typeof document !== 'undefined' && document.hidden) return
    if (!this.host.isOpen()) return
    const now = this.now()
    const silenceMs = this.silenceWindowMs()
    if (now - this.lastFrameAt <= silenceMs) {
      // Live since open for a whole window: forget the earlier silent streak.
      if (now - this.silenceClockStartedAt > silenceMs) this.silentReconnects = 0
      return
    }
    if (now < this.graceUntil) return
    this.silentReconnects += 1
    this.host.onDead()
  }

  private handleVisibility(): void {
    const now = this.now()
    if (typeof document !== 'undefined' && document.hidden) {
      this.hiddenAt = now
      return
    }
    // Move each stamp past only the hidden time after it, and never past now: a
    // stamp from before the hide keeps its visible age, and a frame that arrived
    // while hidden counts as arriving at the return.
    const skipHidden = (stamp: number) => stamp + now - Math.max(this.hiddenAt, stamp)
    this.lastFrameAt = skipHidden(this.lastFrameAt)
    this.silenceClockStartedAt = skipHidden(this.silenceClockStartedAt)
    this.hiddenAt = 0
    // Two checks of grace for a thawed socket, none for one whose visible silence
    // had already passed the window before it was hidden.
    this.graceUntil = now - this.lastFrameAt > this.silenceWindowMs()
      ? 0
      : now + WS_SILENCE_CHECK_MS * 2
  }
}
