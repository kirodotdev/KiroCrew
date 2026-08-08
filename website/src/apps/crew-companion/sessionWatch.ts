/**
 * Watch the gateway for a chat session's turn ending, so the companion can say so.
 *
 * ## Why a WebSocket and not the companion's own backend
 *
 * The rest of the companion learns things by polling its own backend, which owns
 * reminders and break nudges. Session completion is different: the backend has no
 * knowledge of it. There is no `session_done` event and nothing enqueues a
 * session-shaped fire — the whole producing half was missing, so the panel's "Tell me
 * when sessions are done" switch saved a value that nothing read.
 *
 * The signal already exists, one layer up: the gateway broadcasts turn lifecycle over
 * the dashboard WebSocket to EVERY connected client, and an app-window page is such a
 * client — same origin, authenticated by the same session cookie. Mochi's panel
 * already consumes it exactly this way (`apps/mochi/panel/panelBridge.ts`), which is
 * the precedent this module follows rather than inventing a backend producer.
 *
 * ## The event names matter, because the obvious guess is wrong
 *
 *   `chat_status` → a turn STARTED (the model stream opened)
 *   `chat_done`   → that turn FINISHED. This is the session-done signal.
 *   `slots`       → the full slot list, each with `running` and `title`
 *   `subagent_done` → a spawned background agent ended. A DIFFERENT thing, ignored.
 *
 * ## The one thing the gateway does not tell us
 *
 * No payload carries a turn's start time or elapsed duration. So "was this turn long
 * enough to be worth interrupting for" can only be answered by having WATCHED the
 * start. A page that connects mid-turn cannot recover when that turn began, which is
 * exactly the `assumedStart` case the ported gate already reasons about: the desktop
 * app hit the same wall and treated an unobserved start as unknown rather than as
 * zero, because measuring from "when I first noticed" silently swallowed every
 * notification after a restart.
 */
import {
  evaluateCompletion,
  confirmAssumedStart,
  type GateDecision,
} from './completionGate'

/** What the caller needs in order to raise a bubble. */
export interface SessionDone {
  /** Slot key of the session that finished. */
  slot: string
  /** The session's title, when the gateway has told us one. */
  title: string
  /** How long the turn ran, when we saw it start. */
  elapsedMs: number
  /**
   * True when the turn ENDED BADLY.
   *
   * The gateway has no "failed" event: a broken turn emits a `chat_message` with
   * `role: 'error'` and then terminates with an ordinary `chat_done`. So without
   * tracking that message a failure is indistinguishable from success, and the
   * companion cheerfully reports a finish that did not happen.
   */
  failed: boolean
}

export interface SessionWatchOptions {
  /** Raise the bubble. Called only for completions that pass the gate. */
  onDone: (done: SessionDone) => void
  /** Read the live preference each time, so toggling it takes effect at once. */
  isSilent: () => boolean
  /** Injectable for tests. */
  now?: () => number
  /** Injectable for tests; defaults to the real same-origin dashboard socket. */
  connect?: () => WebSocket
  /**
   * The companion's backend says a fire was just queued.
   *
   * A doorbell, not the delivery: the caller drains `/pending` itself, so the
   * cursor stays the single authority on ordering and nothing can arrive twice or
   * out of order. Without this the overlay waited out its poll interval on top of
   * the backend's own tick, and a due reminder was visibly late.
   */
  onFireQueued?: () => void
  /**
   * A tool is BLOCKED waiting for the user to approve it.
   *
   * This is the create half of the approval lifecycle, and it was the missing one:
   * the gateway broadcasts an `approval` frame the moment a tool goes pending, but
   * nothing here listened, so the sticky approval bubble the display machinery can
   * already render was never actually raised — and the panel's promise that
   * "anything waiting on you always notifies" quietly did not hold. The resolve half
   * (`onApprovalResolved`) was wired; without this the slot could be released but
   * never claimed.
   *
   * The frame carries the approval's `slot` (see state.py's broadcast_ws('approval',
   * …)) but no human title, so the words come from the same per-slot `titles` table
   * that `onDone` reads — the last `slots`/title we saw for that session, or '' when
   * we joined too late to have one.
   */
  onApproval?: (blocked: { slot: string; title: string }) => void
  /**
   * Blocked work was resolved somewhere else — approved or rejected in the
   * dashboard rather than through the companion's own bubble.
   *
   * Without this, a sticky bubble keeps holding the notification slot until the
   * user clicks it or the bounded hold expires, so everything behind it waits on a
   * question that has already been answered.
   */
  onApprovalResolved?: () => void
}

/** The gateway's socket, same origin as this page. */
function defaultConnect(): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return new WebSocket(`${proto}//${window.location.host}/api/ws`)
}

/** Backoff bounds for reconnection. */
const RECONNECT_MIN_MS = 1_000
const RECONNECT_MAX_MS = 30_000

/**
 * Start watching. Returns a stop function.
 *
 * Deliberately not a React hook: it owns a socket and a table of start times, and
 * both must survive re-renders of the companion. The caller holds the stop function.
 */
export function watchSessions(opts: SessionWatchOptions): () => void {
  const now = opts.now ?? (() => Date.now())
  const connect = opts.connect ?? defaultConnect

  /** slot -> when we SAW it start. Absent means we never saw this turn begin. */
  const startedAt = new Map<string, number>()
  /**
   * Slots whose start we only assumed — they were already running the first time we
   * heard about them, so their real start is unknown and the duration rule cannot be
   * applied to them fairly.
   */
  const assumed = new Set<string>()
  /** slot -> last known title, for the bubble's words. */
  const titles = new Map<string, string>()
  /** Slots that emitted an error message during the current turn. */
  const failedSlots = new Set<string>()
  /**
   * Slots the USER deliberately stopped mid-turn.
   *
   * The backend broadcasts `chat_done` for a stopped turn exactly as it does for a
   * finished one — the frame is `{slot}` either way — so without this the companion
   * celebrated an interruption as a success. The stop itself never arrives as a
   * `chat_message` (the stop card is appended without a broadcast), but the `slots`
   * frames this watcher already consumes carry `stopping: true` while the cancel is
   * in flight; that is the signal recorded here. A stopped turn is neither a success
   * nor a failure — the user chose to end it — so `finish()` drops it silently:
   * no hop, no bubble, no error shake.
   */
  const stoppedSlots = new Set<string>()

  let socket: WebSocket | null = null
  let stopped = false
  let retryMs = RECONNECT_MIN_MS
  let retryTimer: number | null = null

  const markStart = (slot: string, wasAssumed: boolean) => {
    if (startedAt.has(slot)) return
    startedAt.set(slot, now())
    if (wasAssumed) assumed.add(slot)
  }

  const finish = (slot: string) => {
    const started = startedAt.get(slot)
    const wasAssumed = assumed.has(slot)
    const failed = failedSlots.has(slot)
    const userStopped = stoppedSlots.has(slot)
    startedAt.delete(slot)
    assumed.delete(slot)
    failedSlots.delete(slot)
    stoppedSlots.delete(slot)

    // The user pressed Stop: this turn ended because they ended it. Celebrating it
    // as done misreports the outcome, and shaking about it misreports it worse.
    if (userStopped) return

    let decision: GateDecision = evaluateCompletion({
      slotKey: slot,
      startedAt: started,
      now: now(),
      assumedStart: wasAssumed,
      /*
       * A FAILURE is never silenced.
       *
       * "Tell me when sessions are done" is a preference about good news. Work that
       * stopped because it broke is the case the user most needs to hear about, and
       * the app this was ported from made the same exception explicitly. The duration
       * rule still applies either way — a two-second failure is still noise.
       */
      silent: failed ? false : opts.isSilent(),
    })

    /*
     * An assumed start cannot be verified here.
     *
     * The desktop app recovered the real start from the gateway's slot history. This
     * page could fetch `/api/chat/slots/<slot>` and read the last user message's
     * timestamp, but that is the only proxy available and it is not the same fact:
     * a queued turn starts well after the message that asked for it. Rather than
     * dress an approximation up as a measurement, treat the elapsed we have as the
     * best available and let the gate rule on it — `confirmAssumedStart` applies the
     * same threshold, so a genuinely short turn is still skipped.
     */
    if (decision.action === 'verify') {
      // (realStart, now, fallbackElapsedMs). No recovered start is available here, so
      // 0 says "nothing to recover" and the measured elapsed is what gets ruled on.
      // Passing only the elapsed would leave the other two undefined, and
      // `now - undefined` is NaN — which is not less than the threshold, so the gate
      // would fall through to NOTIFY on every assumed start. Failing towards a
      // notification is the wrong direction for a rule whose whole job is silence.
      decision = confirmAssumedStart(0, now(), decision.elapsedMs)
    }
    if (decision.action !== 'notify') return

    opts.onDone({
      slot,
      title: titles.get(slot) ?? '',
      elapsedMs: decision.elapsedMs,
      failed,
    })
  }

  const handle = (raw: string) => {
    let msg: { type?: string; data?: unknown }
    try {
      msg = JSON.parse(raw) as { type?: string; data?: unknown }
    } catch {
      return // a frame we cannot read is not a reason to tear the socket down
    }
    const data = (msg.data ?? {}) as Record<string, unknown>

    switch (msg.type) {
      case 'chat_status': {
        // The earliest live start marker there is.
        if (typeof data.slot === 'string') markStart(data.slot, false)
        break
      }
      case 'slots': {
        // Carries `running` and `title` for every session. A slot found already
        // running is an ASSUMED start: we joined mid-turn.
        const list = Array.isArray(data) ? data : (data.slots as unknown[]) ?? []
        for (const entry of list) {
          const s = entry as { key?: unknown; running?: unknown; title?: unknown; stopping?: unknown }
          if (typeof s.key !== 'string') continue
          if (typeof s.title === 'string') titles.set(s.key, s.title)
          if (s.running === true) markStart(s.key, true)
          /*
           * `stopping: true` is the only signal this socket gets that the user
           * pressed Stop — the stop card itself is appended to the transcript
           * without a `chat_message` broadcast, so it never arrives here. The flag
           * is transient (the cancel is in flight), which is exactly why it is
           * RECORDED rather than acted on: by the time `chat_done` lands the flag
           * may already be false again, and `finish()` needs to know the turn's
           * ending was chosen, not earned.
           */
          if (s.stopping === true) stoppedSlots.add(s.key)
        }
        break
      }
      case 'chat_done': {
        if (typeof data.slot === 'string') finish(data.slot)
        break
      }
      case 'chat_message': {
        // The only signal that a turn is going wrong. Recorded rather than acted on:
        // the turn is still running, and `chat_done` is what closes it.
        if (typeof data.slot === 'string' && data.role === 'error') {
          failedSlots.add(data.slot)
        }
        break
      }
      case 'approval': {
        // A tool just went pending on the user's OK. Carries a `slot` (unlike the
        // resolve frame, which carries only an id), so this CAN say which session is
        // waiting — the title comes from the same table `onDone` reads, since the
        // frame itself has no human name.
        if (typeof data.slot === 'string') {
          opts.onApproval?.({ slot: data.slot, title: titles.get(data.slot) ?? '' })
        }
        break
      }
      case 'approval_resolved': {
        // Carries the approval's id, not a slot — so this cannot say WHICH bubble to
        // clear. Treated as "the blocking question was answered", which is enough for
        // the caller to release a sticky bubble that is holding the slot.
        opts.onApprovalResolved?.()
        break
      }
      case 'app_event': {
        /*
         * App-published events all ride under ONE ws type with the real name
         * inside, because an app event called e.g. "notification" would otherwise
         * land in the dashboard's own notification feed. So the name has to be
         * unwrapped here rather than switched on above.
         */
        // The name is in `event`, NOT `type`: apps/event_bus.py builds the
        // envelope by DELETING `type` and writing the real name to `event`, so a
        // check on `inner.type` compares against undefined forever. It type-checks,
        // it throws nothing, and the doorbell simply never rings — reminders fall
        // back to the 2s poll and look merely a little slow. Mochi's panelBridge
        // carries the same note for the same reason.
        const inner = data as { event?: unknown; app?: unknown }
        if (inner.app === 'crew-companion' && inner.event === 'crew-companion:fire') {
          opts.onFireQueued?.()
        }
        break
      }
      default:
        break
    }
  }

  /**
   * The socket dropped while turns were in flight — report each as a FAILURE.
   *
   * A gateway restart or a network drop means no `chat_done` will ever arrive for
   * those slots, so without this they sat in `startedAt` forever: no notification (the
   * user is never told the work died) AND a stale start time, so the NEXT turn on that
   * slot measured its duration from the old start and could report a three-second turn
   * as a long one.
   *
   * Reported as failed, not silently dropped, because that is what it is: the work
   * stopped without finishing and the user did not ask for that. It goes through the
   * same `finish(failed)` path as an error row — which also means it is never silenced
   * by the session-notifications preference, matching the existing rule that bad news
   * always gets through.
   *
   * A deliberate Stop is excluded: `finish()` returns early for a slot in
   * `stoppedSlots`, so a stop that happens to be followed by a disconnect stays quiet.
   */
  const failInFlight = () => {
    for (const slot of [...startedAt.keys()]) {
      failedSlots.add(slot)
      finish(slot)
    }
  }

  const open = () => {
    if (stopped) return
    let ws: WebSocket
    try {
      ws = connect()
    } catch {
      schedule()
      return
    }
    socket = ws
    ws.onopen = () => { retryMs = RECONNECT_MIN_MS }
    ws.onmessage = (ev) => handle(String(ev.data))
    // Both paths reconnect: a gateway restart closes cleanly, a network drop errors.
    ws.onclose = () => { socket = null; failInFlight(); schedule() }
    ws.onerror = () => { try { ws.close() } catch { /* already closing */ } }
  }

  function schedule() {
    if (stopped || retryTimer !== null) return
    retryTimer = window.setTimeout(() => {
      retryTimer = null
      retryMs = Math.min(retryMs * 2, RECONNECT_MAX_MS)
      open()
    }, retryMs)
  }

  open()

  return () => {
    stopped = true
    if (retryTimer !== null) window.clearTimeout(retryTimer)
    retryTimer = null
    if (socket) {
      // Drop the handler first: closing fires onclose, which would otherwise
      // schedule a reconnect for a watcher the caller has already stopped.
      socket.onclose = null
      try { socket.close() } catch { /* already closing */ }
      socket = null
    }
  }
}
