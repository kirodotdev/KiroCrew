/**
 * The live-browser store: one place that holds what the gateway relay says
 * about the agent's browser, per session, and the hooks the card reads it
 * through.
 *
 * Two inputs, both from the dashboard websocket (`useWebSocket`):
 *   - `browser_live` JSON events: the state machine (waiting_allow, live,
 *     in_control, done, login_needed, error), the page, the browser, the step
 *     strip and the stream's current rung and fps. `applyEvent` folds one in.
 *   - binary frames (`useBrowserLive.frame.ts`): `applyFrame` acks each one
 *     ONCE, keeps it in the turn's ring for Replay and hands it to whichever
 *     canvas is painting that turn, outside React's render cycle.
 *
 * Why a module store and not Redux: `TurnBlock` is store-free by design (the
 * app-sdk embeds it with no Provider), the frame path must not re-render a
 * React tree 60 times a second, and the frames themselves are bytes React has
 * no business holding. `useSyncExternalStore` gives the card a subscription
 * with none of that.
 *
 * Turn binding. The relay names turns by its own `turn_id`; the transcript
 * knows a turn by its rows. The card for a turn anchors on that turn's FIRST
 * `browser` tool row and binds to the session's current live record the first
 * time it mounts while the turn is running. The binding is remembered here, so
 * the virtualised transcript can unmount and remount the card without losing
 * which record is its own. A settled turn that was never bound (the page was
 * reloaded, or the card mounted after the turn ended) binds to the newest
 * unbound record only when that record's clock overlaps the anchor row's
 * timestamp; otherwise it shows nothing rather than another turn's browsing.
 */

import { useSyncExternalStore } from 'react'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { BROWSER_HANDBACK_CHAT_EVENT, type BrowserHandbackSaid } from '../api/client/browserLive'
import type { BrowserInputEvent } from './useBrowserLive.input'
import { FrameRing, decodeBrowserFrame, type DecodedBrowserFrame } from './useBrowserLive.frame'
import { readQuickAsk, type QuickAsk, type QuickAskAction, type QuickAskAnswer, type QuickAskValues } from '../pages/chat/quickAsk/types'

export type BrowserLiveState = 'waiting_allow' | 'live' | 'in_control' | 'done' | 'login_needed' | 'error'
export type BrowserKind = 'user_chrome' | 'edge' | 'kiro_chromium'

export interface BrowserLiveStep {
  label: string
  status: 'done' | 'active' | 'pending'
  /** Reported while a loop op (`reflex`) ran: the strip may fold a run of these. */
  loop?: boolean
}

/**
 * A loop op's counters (the `browser_live` event's `loop`): `stop` is null while
 * it runs, else the loop's own short reason token. Any counter may be null.
 */
export interface BrowserLiveLoop {
  moves: number | null
  rate: number | null
  steps: number | null
  maxSteps: number | null
  seconds: number | null
  maxSeconds: number | null
  stop: string | null
}

/** The hand-back note's cap, in characters (the gateway's `HANDBACK_NOTE_MAX`). */
export const HANDBACK_NOTE_MAX = 500

/**
 * What the card saw happen, with the wall-clock ms it saw it: a new step label
 * or a new page URL. The replay timeline falls back to this when the gateway
 * kept no recording (an older gateway, a 404), so its markers still land where
 * the agent acted.
 */
export interface BrowserLiveLogEntry { ts: number; op: 'navigate' | 'other'; label: string; url: string }

/** Entries a turn's log keeps; a long turn drops its oldest. */
export const LIVE_LOG_MAX = 500

/** The `browser_live` websocket event, as the wire contract spells it. */
export interface BrowserLiveEvent {
  session_key: string
  turn_id: string
  state: BrowserLiveState
  url?: string
  title?: string
  browser?: BrowserKind
  headless?: boolean
  step?: { index: number; label: string }
  steps?: BrowserLiveStep[]
  rung?: number
  fps?: number
  /** Server-authored detail for `error`; rendered verbatim, never translated. */
  detail?: string
  /** The card's turn has ended. With a live state this is a REOPENED view. */
  turn_over?: boolean
  /** The tab the card showed: still `open` (Open live works), `closed`, or none. */
  tab?: BrowserLiveTab
  loop?: BrowserLiveLoop | null
  /** The hand-back choices the agent's waiting op offered; empty: the card's own. */
  handback_options?: string[]
  /** The quick ask the agent's browser is waiting on, or null once there is none. */
  ask?: QuickAsk | null
}

export type BrowserLiveTab = '' | 'open' | 'closed'

/** One turn's browsing, as the card sees it. Replaced (not mutated) on every event. */
export interface BrowserLiveTurn {
  sessionKey: string
  turnId: string
  state: BrowserLiveState
  url: string
  title: string
  browser: BrowserKind | null
  headless: boolean
  step: { index: number; label: string } | null
  steps: BrowserLiveStep[]
  rung: number
  fps: number
  detail: string
  /** Wall-clock ms the first event arrived, and the ms of `done` / `error`. */
  startedAt: number
  endedAt: number | null
  /** True once a frame has landed, so the card can swap its placeholder for the canvas. */
  hasFrame: boolean
  /** The frames kept for Replay. Shared by every copy of this record. */
  ring: FrameRing
  /**
   * The gateway's own text for the last control write (take over, hand back,
   * input) that failed, for the card's ErrorNotice; empty when the last one
   * succeeded or the server has since moved the state. Rendered verbatim.
   */
  controlError: string
  /** The turn is over; a live state then means the person reopened the card. */
  turnOver: boolean
  /** Whether the tab the card showed is still there for "Open live". */
  tab: BrowserLiveTab
  /** Step and URL changes as they arrived (see `BrowserLiveLogEntry`). */
  log: BrowserLiveLogEntry[]
  /** The loop op's counters while one runs or just stopped, else null. */
  loop: BrowserLiveLoop | null
  /** The hand-back choices the agent offered (empty: the card shows its own). */
  handbackOptions: string[]
  /** The quick ask waiting for the person (see `pages/chat/quickAsk`), else null. */
  ask: QuickAsk | null
}

interface SessionRecord {
  current: BrowserLiveTurn | null
  turns: Map<string, BrowserLiveTurn>
  /** The stream the ack high-water mark belongs to; a new stream resets it. */
  ackedStream: string
  /** The highest seq of `ackedStream` acked, so a re-delivered frame is not acked twice. */
  ackedSeq: number
  /**
   * Card instances currently on screen, by instance id, each with the turn it
   * shows. Only instances bound to the CURRENT turn count towards the stream's
   * visibility: a finished turn's card stays on screen for as long as the
   * transcript does, and it must not keep a later turn's screencast running
   * while that turn's own card is off screen.
   */
  visibleInstances: Map<string, string>
  /** The last visibility the gateway was told. */
  reportedVisible: boolean | null
  /** Frames of the current turn received while no card instance shows that turn. */
  unseenFrames: number
}

type Listener = () => void
type FrameListener = (frame: DecodedBrowserFrame) => void

const STATES: ReadonlySet<string> = new Set(['waiting_allow', 'live', 'in_control', 'done', 'login_needed', 'error'])
const BROWSERS: ReadonlySet<string> = new Set(['user_chrome', 'edge', 'kiro_chromium'])

/** How far an anchor row's timestamp may sit from a record's clock and still claim it. */
const BIND_SLACK_MS = 120_000

/**
 * Turns per session whose replay ring is kept. A ring holds up to 48 MB, and a
 * dashboard tab that stays open through a day of browsing would otherwise keep
 * one per turn for the life of the page; older turns keep their record and
 * their last state, just not their frames.
 */
export const REPLAY_RINGS_KEPT = 2

/** Frames a turn may receive with no card showing it before the session is reported hidden. */
export const UNSEEN_FRAMES_BEFORE_PAUSE = 30
/**
 * While the person holds the browser and the card is on screen, the gateway is
 * told so again this often. A static page sends no frames (so no acks) and
 * visibility is only posted on change, so without this a person reading a page
 * through a takeover that outlived its turn would look gone to the gateway's
 * idle bound (`TAKEOVER_IDLE_S`, 5 minutes) and be handed back.
 */
export const TAKEOVER_HEARTBEAT_MS = 60_000

/** The text a failed write leaves for the card: the gateway's own, else the error's. */
function errorText(e: unknown): string {
  if (e instanceof Error && e.message) return e.message
  return e ? String(e) : i18nT('pages.chatPage.unknown_error')
}

function readSteps(raw: unknown): BrowserLiveStep[] {
  if (!Array.isArray(raw)) return []
  const out: BrowserLiveStep[] = []
  for (const s of raw) {
    if (!s || typeof s !== 'object') continue
    const { label, status } = s as Record<string, unknown>
    if (typeof label !== 'string') continue
    const step: BrowserLiveStep = { label, status: status === 'done' || status === 'active' ? status : 'pending' }
    if ((s as Record<string, unknown>).loop === true) step.loop = true
    out.push(step)
  }
  return out
}

const count = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null)

/** The event's `loop`, field by field; null for anything that is not one. */
export function readLoop(raw: unknown): BrowserLiveLoop | null {
  if (!raw || typeof raw !== 'object') return null
  const d = raw as Record<string, unknown>
  return {
    moves: count(d.moves),
    rate: count(d.rate),
    steps: count(d.steps),
    maxSteps: count(d.max_steps),
    seconds: count(d.seconds),
    maxSeconds: count(d.max_seconds),
    stop: typeof d.stop === 'string' && d.stop ? d.stop : null,
  }
}

function readOptions(raw: unknown): string[] {
  if (!Array.isArray(raw)) return []
  return raw.filter((o): o is string => typeof o === 'string' && !!o.trim()).map(o => o.trim().slice(0, 40)).slice(0, 4)
}

/** Check a wire event field by field; `null` for one the card must not act on. */
export function readBrowserLiveEvent(raw: unknown): BrowserLiveEvent | null {
  if (!raw || typeof raw !== 'object') return null
  const d = raw as Record<string, unknown>
  if (typeof d.session_key !== 'string' || !d.session_key) return null
  if (typeof d.turn_id !== 'string' || !d.turn_id) return null
  if (typeof d.state !== 'string' || !STATES.has(d.state)) return null
  const step = d.step && typeof d.step === 'object' && typeof (d.step as Record<string, unknown>).label === 'string'
    ? { index: Number((d.step as Record<string, unknown>).index) || 0, label: String((d.step as Record<string, unknown>).label) }
    : undefined
  return {
    session_key: d.session_key,
    turn_id: d.turn_id,
    state: d.state as BrowserLiveState,
    url: typeof d.url === 'string' ? d.url : undefined,
    title: typeof d.title === 'string' ? d.title : undefined,
    browser: typeof d.browser === 'string' && BROWSERS.has(d.browser) ? d.browser as BrowserKind : undefined,
    headless: typeof d.headless === 'boolean' ? d.headless : undefined,
    step,
    steps: d.steps === undefined ? undefined : readSteps(d.steps),
    rung: typeof d.rung === 'number' && Number.isFinite(d.rung) ? d.rung : undefined,
    fps: typeof d.fps === 'number' && Number.isFinite(d.fps) ? d.fps : undefined,
    detail: typeof d.detail === 'string' ? d.detail : undefined,
    turn_over: typeof d.turn_over === 'boolean' ? d.turn_over : undefined,
    tab: d.tab === 'open' || d.tab === 'closed' || d.tab === '' ? d.tab : undefined,
    loop: d.loop === undefined ? undefined : readLoop(d.loop),
    handback_options: d.handback_options === undefined ? undefined : readOptions(d.handback_options),
    ask: d.ask === undefined ? undefined : readQuickAsk(d.ask),
  }
}

const EMPTY_TURNS: readonly BrowserLiveTurn[] = []

const isEnded = (s: BrowserLiveState) => s === 'done' || s === 'error'

/** Map keys are `session + SEP + id`; a NUL cannot appear in either half. */
const SEP = '\u0000'
const joinKey = (a: string, b: string) => a + SEP + b

/** The gateway's `code` for a refused write (`{"error","code"}` body), or ''. */
function errorCode(e: unknown): string {
  const body = (e as { body?: unknown } | null)?.body
  if (typeof body !== 'string' || !body) return ''
  try {
    const code = (JSON.parse(body) as { code?: unknown }).code
    return typeof code === 'string' ? code : ''
  } catch {
    return ''
  }
}

/** Swallow a write's rejection: the card's writes are best effort, and the
 *  journal already has the `ApiError`. */
const quiet = (p: Promise<unknown>) => { p.catch(() => {}) }

class BrowserLiveStore {
  private sessions = new Map<string, SessionRecord>()
  private anchors = new Map<string, string>()
  private listeners = new Set<Listener>()
  private frameListeners = new Map<string, Set<FrameListener>>()
  private documentListening = false
  private heartbeat: ReturnType<typeof setInterval> | null = null
  /** Injected for tests; production uses the facade's methods. */
  private writes = {
    ack: (k: string, seq: number, stream: string) => api.browserLiveAck(k, seq, stream) as Promise<unknown>,
    visibility: (k: string, v: boolean) => api.browserLiveVisibility(k, v) as Promise<unknown>,
    claim: (k: string) => api.browserInputClaim(k) as Promise<unknown>,
    release: (k: string, said?: BrowserHandbackSaid) => api.browserInputRelease(k, said) as Promise<unknown>,
    input: (k: string, e: BrowserInputEvent) => api.browserInputEvent(k, e) as Promise<unknown>,
    open: (k: string) => api.browserLiveOpenInBrowser(k) as Promise<unknown>,
    reopen: (k: string, turnId: string) => api.browserLiveReopen(k, turnId) as Promise<unknown>,
    close: (k: string) => api.browserLiveClose(k) as Promise<unknown>,
    answer: (body: Parameters<typeof api.browserAskAnswer>[0]) => api.browserAskAnswer(body) as Promise<unknown>,
  }

  private session(key: string): SessionRecord {
    let s = this.sessions.get(key)
    if (!s) {
      s = { current: null, turns: new Map(), ackedStream: '', ackedSeq: -1, visibleInstances: new Map(), reportedVisible: null, unseenFrames: 0 }
      this.sessions.set(key, s)
    }
    return s
  }

  /** Empty the rings of every turn but the newest `REPLAY_RINGS_KEPT`. */
  private evictRings(s: SessionRecord): void {
    const ids = [...s.turns.keys()]
    for (const id of ids.slice(0, Math.max(0, ids.length - REPLAY_RINGS_KEPT))) {
      s.turns.get(id)?.ring.clear()
    }
  }

  private notify(): void {
    this.turnLists.clear()
    for (const l of this.listeners) l()
  }

  /** Cached per notify, so `useSyncExternalStore` sees a stable snapshot. */
  private turnLists = new Map<string, readonly BrowserLiveTurn[]>()

  /** Every turn the page has seen for a session, oldest first. */
  sessionTurns(sessionKey: string | null): readonly BrowserLiveTurn[] {
    if (!sessionKey) return EMPTY_TURNS
    const cached = this.turnLists.get(sessionKey)
    if (cached) return cached
    const s = this.sessions.get(sessionKey)
    const list = s ? [...s.turns.values()] : EMPTY_TURNS
    this.turnLists.set(sessionKey, list)
    return list
  }

  /** One turn's record by id. */
  turn(sessionKey: string | null, turnId: string | null): BrowserLiveTurn | null {
    if (!sessionKey || !turnId) return null
    return this.sessions.get(sessionKey)?.turns.get(turnId) ?? null
  }

  subscribe = (l: Listener): (() => void) => {
    this.listeners.add(l)
    return () => { this.listeners.delete(l) }
  }

  // ---- inbound ------------------------------------------------------------

  /** Fold one `browser_live` event in. Unknown or malformed events are dropped. */
  applyEvent(raw: unknown, now: number = Date.now()): void {
    const ev = readBrowserLiveEvent(raw)
    if (!ev) return
    const s = this.session(ev.session_key)
    const prev = s.turns.get(ev.turn_id)
    // A new turn ends the one before it: the relay streams one turn at a time.
    if (!prev && s.current && !isEnded(s.current.state)) {
      const closed: BrowserLiveTurn = { ...s.current, state: 'done', endedAt: now }
      s.turns.set(closed.turnId, closed)
    }
    const turnOver = ev.turn_over ?? (isEnded(ev.state) || (prev?.turnOver ?? false))
    const url = ev.url ?? prev?.url ?? ''
    let log = prev?.log ?? []
    if (url && url !== (prev?.url ?? '')) log = [...log, { ts: now, op: 'navigate' as const, label: url, url }]
    if (ev.step && (ev.step.label !== prev?.step?.label || ev.step.index !== prev?.step?.index)) {
      log = [...log, { ts: now, op: 'other' as const, label: ev.step.label, url }]
    }
    if (log.length > LIVE_LOG_MAX) log = log.slice(log.length - LIVE_LOG_MAX)
    const next: BrowserLiveTurn = {
      sessionKey: ev.session_key,
      turnId: ev.turn_id,
      state: ev.state,
      url,
      title: ev.title ?? prev?.title ?? '',
      browser: ev.browser ?? prev?.browser ?? null,
      headless: ev.headless ?? prev?.headless ?? false,
      step: ev.step ?? (ev.state === 'done' ? null : prev?.step ?? null),
      steps: ev.steps ?? prev?.steps ?? [],
      rung: ev.rung ?? prev?.rung ?? 0,
      fps: ev.fps ?? prev?.fps ?? 0,
      detail: ev.detail ?? (ev.state === 'error' ? prev?.detail ?? '' : ''),
      startedAt: prev?.startedAt ?? now,
      // A reopened view keeps the turn's own end, so the done line's duration is the
      // agent's browsing, not the time the person spent looking afterwards.
      endedAt: isEnded(ev.state) || turnOver ? (prev?.endedAt ?? now) : null,
      hasFrame: prev?.hasFrame ?? false,
      ring: prev?.ring ?? new FrameRing(),
      // A control write's failure text is kept while the server keeps saying the
      // state the write left behind (in_control after a failed hand back, live
      // after a failed take over); a state CHANGE is the server's word on the
      // lock and clears it, as does the turn ending.
      controlError: prev && ev.state === prev.state && !isEnded(ev.state) ? prev.controlError : '',
      turnOver,
      tab: ev.tab ?? prev?.tab ?? '',
      log,
      loop: ev.loop !== undefined ? ev.loop : prev?.loop ?? null,
      handbackOptions: ev.handback_options ?? prev?.handbackOptions ?? [],
      // An ended turn has nothing left to answer.
      ask: isEnded(ev.state) ? null : ev.ask !== undefined ? ev.ask : prev?.ask ?? null,
    }
    s.turns.set(next.turnId, next)
    s.current = next
    if (!prev) {
      this.evictRings(s)
      // A new turn starts with no card showing it; the unseen count is per turn.
      s.unseenFrames = 0
    }
    this.notify()
  }

  /** Keep one decoded frame, ack it once, and paint it where a canvas listens. */
  applyFrame(frame: DecodedBrowserFrame): void {
    const key = frame.header.session_key
    const s = this.session(key)
    // A turn no card shows (another slot's browsing, a Slack-driven turn, a new
    // turn whose card the virtualised transcript has not mounted while an older
    // turn's card is still on screen) tells the gateway so, which stops the
    // screencast instead of streaming at full rate into memory nobody looks at.
    // Not on the first frame: a card that is about to mount reports its
    // visibility a tick after the event that made it, and a frame can beat that
    // tick. A card that mounts later reports true and the cast resumes.
    if (s.current && !this.shownInstances(s)) {
      s.unseenFrames += 1
      if (s.unseenFrames >= UNSEEN_FRAMES_BEFORE_PAUSE) this.reportVisibility(key)
    }
    if (frame.header.stream !== s.ackedStream) {
      // A new stream numbers from 1 again; the old high-water mark must not hold
      // its frames unacked (each would wait the relay's 2 s timeout).
      s.ackedStream = frame.header.stream
      s.ackedSeq = -1
    }
    if (frame.header.seq > s.ackedSeq) {
      s.ackedSeq = frame.header.seq
      quiet(this.writes.ack(key, frame.header.seq, frame.header.stream ?? ''))
    }
    const turn = s.current
    // An ended turn takes no more frames, but one still in flight when the turn
    // ended (a quick turn whose page painted late) is kept if the turn has none:
    // it is the done card's thumbnail instead of a globe.
    if (!turn || (isEnded(turn.state) && turn.hasFrame)) return
    turn.ring.push(frame)
    if (!turn.hasFrame) {
      const withFrame = { ...turn, hasFrame: true }
      s.turns.set(turn.turnId, withFrame)
      s.current = withFrame
      this.notify()
    }
    const ls = this.frameListeners.get(joinKey(key, turn.turnId))
    if (ls) for (const l of ls) l(frame)
  }

  /** One binary websocket message: a frame, or nothing. A frame that fails the
   *  header checks is dropped whole (see useBrowserLive.frame). */
  applyBinary(buf: ArrayBuffer): void {
    const frame = decodeBrowserFrame(buf)
    if (frame) this.applyFrame(frame)
  }

  /** Paint callback for one turn. The last frame already held is delivered at once. */
  subscribeFrames(sessionKey: string, turnId: string, cb: FrameListener): () => void {
    const k = joinKey(sessionKey, turnId)
    let set = this.frameListeners.get(k)
    if (!set) { set = new Set(); this.frameListeners.set(k, set) }
    set.add(cb)
    const last = this.sessions.get(sessionKey)?.turns.get(turnId)?.ring.last
    if (last) cb(last)
    return () => {
      set?.delete(cb)
      if (set && set.size === 0) this.frameListeners.delete(k)
    }
  }

  // ---- reads --------------------------------------------------------------

  current(sessionKey: string | null): BrowserLiveTurn | null {
    return sessionKey ? this.sessions.get(sessionKey)?.current ?? null : null
  }

  inControl(sessionKey: string | null): boolean {
    return this.current(sessionKey)?.state === 'in_control'
  }

  /**
   * The record a turn card shows, by the rule in the module comment.
   * `anchorTs` is the anchor row's server timestamp in ms, when it has one.
   */
  resolveTurn(sessionKey: string, anchorKey: string, running: boolean, anchorTs: number | null): BrowserLiveTurn | null {
    const s = this.sessions.get(sessionKey)
    if (!s) return null
    const ak = joinKey(sessionKey, anchorKey)
    const bound = this.anchors.get(ak)
    if (bound) return s.turns.get(bound) ?? null
    const cur = s.current
    if (!cur) return null
    if (running) {
      if ((isEnded(cur.state) || cur.turnOver) && this.isBound(sessionKey, cur.turnId)) return null
      this.anchors.set(ak, cur.turnId)
      return cur
    }
    // Settled turn, never bound: claim the newest unbound FINISHED record only
    // when the clocks agree, so a reload cannot hand an old turn a newer turn's
    // browsing, and a settled turn never shows a turn that is still live.
    if ((!isEnded(cur.state) && !cur.turnOver) || this.isBound(sessionKey, cur.turnId)) return null
    if (anchorTs !== null) {
      const end = cur.endedAt ?? Date.now()
      if (anchorTs < cur.startedAt - BIND_SLACK_MS || anchorTs > end + BIND_SLACK_MS) return null
    }
    this.anchors.set(ak, cur.turnId)
    return cur
  }

  private isBound(sessionKey: string, turnId: string): boolean {
    const prefix = sessionKey + SEP
    for (const [k, v] of this.anchors) if (v === turnId && k.startsWith(prefix)) return true
    return false
  }

  // ---- outbound -----------------------------------------------------------

  /**
   * One card instance reports whether it is on screen, and which turn it shows.
   * The gateway hears the OR over the instances showing the CURRENT turn.
   */
  setVisible(sessionKey: string, instanceId: string, visible: boolean, turnId: string): void {
    const s = this.session(sessionKey)
    if (visible) s.visibleInstances.set(instanceId, turnId)
    else s.visibleInstances.delete(instanceId)
    this.listenToDocument()
    this.reportVisibility(sessionKey)
  }

  /** Whether any on-screen instance shows the session's current turn. */
  private shownInstances(s: SessionRecord): boolean {
    const cur = s.current?.turnId
    if (!cur) return false
    for (const t of s.visibleInstances.values()) if (t === cur) return true
    return false
  }

  private effectiveVisible(s: SessionRecord): boolean {
    const docVisible = typeof document === 'undefined' || document.visibilityState !== 'hidden'
    return docVisible && this.shownInstances(s)
  }

  private reportVisibility(sessionKey: string): void {
    const s = this.session(sessionKey)
    const v = this.effectiveVisible(s)
    if (s.reportedVisible === v) return
    s.reportedVisible = v
    quiet(this.writes.visibility(sessionKey, v))
  }

  /** Re-post `visible: true` for every session the person holds on screen; stops
   *  itself once none is held. */
  private startHeartbeat(): void {
    if (this.heartbeat !== null) return
    this.heartbeat = setInterval(() => {
      let held = false
      for (const [k, s] of this.sessions) {
        if (s.current?.state !== 'in_control') continue
        held = true
        if (this.effectiveVisible(s)) quiet(this.writes.visibility(k, true))
      }
      if (!held && this.heartbeat !== null) {
        clearInterval(this.heartbeat)
        this.heartbeat = null
      }
    }, TAKEOVER_HEARTBEAT_MS)
  }

  private listenToDocument(): void {
    if (this.documentListening || typeof document === 'undefined') return
    this.documentListening = true
    document.addEventListener('visibilitychange', () => {
      for (const k of this.sessions.keys()) this.reportVisibility(k)
    })
  }

  /**
   * Take over. The state flips at once and reverts if the gateway refuses; the
   * refusal's text goes on the record so the card can say why the click did
   * nothing (Take over stays as the retry).
   */
  async claim(sessionKey: string): Promise<boolean> {
    const s = this.session(sessionKey)
    const before = s.current
    if (!before || isEnded(before.state)) return false
    this.setState(sessionKey, 'in_control', '')
    try {
      await this.writes.claim(sessionKey)
      this.startHeartbeat()
      return true
    } catch (e) {
      if (s.current?.turnId === before.turnId) this.setState(sessionKey, before.state, errorText(e))
      return false
    }
  }

  /**
   * Hand back. Optimistic the same way, and UNDONE if the gateway refuses: the
   * server's lock is still held then, so the card must keep saying so (and keep
   * offering Hand back) rather than show the agent in control of a browser whose
   * every op the server refuses. The failure text goes on the record for the
   * card's ErrorNotice.
   */
  async release(sessionKey: string, said?: BrowserHandbackSaid): Promise<boolean> {
    const s = this.session(sessionKey)
    const before = s.current
    if (before?.state !== 'in_control') return false
    this.setState(sessionKey, 'live', '')
    const note = said?.note?.trim().slice(0, HANDBACK_NOTE_MAX) || undefined
    const choice = said?.choice?.trim().slice(0, 40) || undefined
    const body = note || choice ? { ...(note ? { note } : {}), ...(choice ? { choice } : {}) } : undefined
    try {
      const answer = await (body ? this.writes.release(sessionKey, body) : this.writes.release(sessionKey))
      if (body && (answer as { delivered?: unknown } | null)?.delivered === 'chat') {
        // No agent op is parked to read it (the turn is over, or the agent is
        // between ops): it becomes the person's next chat message, exactly as if
        // typed in the composer.
        const text = [choice, note].filter(Boolean).join(': ')
        window.dispatchEvent(new CustomEvent(BROWSER_HANDBACK_CHAT_EVENT, { detail: { slotKey: sessionKey, text } }))
      }
      return true
    } catch (e) {
      if (s.current?.turnId === before.turnId) this.setState(sessionKey, 'in_control', errorText(e))
      return false
    }
  }

  /** One takeover event. A refusal is kept for the card; the keystroke itself never is. */
  sendInput(sessionKey: string, event: BrowserInputEvent): Promise<unknown> {
    return this.writes.input(sessionKey, event).catch((e: unknown) => {
      const s = this.session(sessionKey)
      if (s.current?.state === 'in_control') this.setState(sessionKey, 'in_control', errorText(e))
      return undefined
    })
  }

  /** Bring the agent's browser window forward. A failure is kept as the card's control error. */
  openInBrowser(sessionKey: string): Promise<void> {
    return this.writes.open(sessionKey).then(() => undefined, (e: unknown) => this.failControl(sessionKey, e))
  }

  /** Whether `turnId` is the session's newest record: only that card can reopen. */
  isCurrentTurn(sessionKey: string, turnId: string): boolean {
    return this.sessions.get(sessionKey)?.current?.turnId === turnId
  }

  /**
   * The finished card's "Open live". The server's `browser_live` event is what
   * turns the card live; this only waits for the answer. `tab_gone` marks the
   * record's tab closed (the card says so and keeps Replay); any other refusal
   * is kept as the record's control error.
   */
  async reopen(sessionKey: string, turnId: string): Promise<boolean> {
    try {
      await this.writes.reopen(sessionKey, turnId)
      return true
    } catch (e) {
      const s = this.session(sessionKey)
      const rec = s.turns.get(turnId)
      if (rec) {
        const gone = errorCode(e) === 'tab_gone'
        const next = { ...rec, tab: gone ? 'closed' as const : rec.tab, controlError: gone ? '' : errorText(e) }
        s.turns.set(turnId, next)
        if (s.current?.turnId === turnId) s.current = next
        this.notify()
      }
      return false
    }
  }

  /**
   * Answer a quick ask. `values` go into this one POST and nowhere else: not
   * the record, not the error text, not a log. A refusal resolves with the
   * gateway's `error` code (or `send_failed`), never throws.
   */
  async answerAsk(sessionKey: string, askId: string, action: QuickAskAction, values?: QuickAskValues): Promise<QuickAskAnswer> {
    try {
      const r = await this.writes.answer({ session_key: sessionKey, ask_id: askId, action, ...(values ? { values } : {}) })
      const d = (r ?? {}) as { ok?: unknown; outcome?: unknown; error?: unknown }
      return {
        ok: d.ok === true,
        ...(typeof d.outcome === 'string' ? { outcome: d.outcome } : {}),
        ...(d.ok === true ? {} : { error: typeof d.error === 'string' && d.error ? d.error : 'send_failed' }),
      }
    } catch (e) {
      const body = (e as { body?: unknown } | null)?.body
      let code = ''
      if (typeof body === 'string' && body) {
        try {
          const parsed = JSON.parse(body) as { error?: unknown; code?: unknown }
          code = typeof parsed.error === 'string' ? parsed.error : typeof parsed.code === 'string' ? parsed.code : ''
        } catch { /* not JSON */ }
      }
      return { ok: false, error: code || 'send_failed' }
    }
  }

  /** Fold a reopened view back to its done line (the server's event does the folding). */
  closeLive(sessionKey: string): Promise<void> {
    return this.writes.close(sessionKey).then(() => undefined, (e: unknown) => this.failControl(sessionKey, e))
  }

  /** Keep a refused card action on the current record, where the card's ErrorNotice reads it. */
  private failControl(sessionKey: string, e: unknown): void {
    const s = this.session(sessionKey)
    if (s.current) this.setState(sessionKey, s.current.state, errorText(e))
  }

  private setState(sessionKey: string, state: BrowserLiveState, controlError?: string): void {
    const s = this.session(sessionKey)
    if (!s.current) return
    const next = { ...s.current, state, controlError: controlError ?? s.current.controlError }
    if (next.state === s.current.state && next.controlError === s.current.controlError) return
    s.turns.set(next.turnId, next)
    s.current = next
    this.notify()
  }

  // ---- tests --------------------------------------------------------------

  /** Replace the writes (tests). Returns the previous set so a test can restore it. */
  __setWritesForTests(w: Partial<BrowserLiveStore['writes']>): BrowserLiveStore['writes'] {
    const prev = this.writes
    this.writes = { ...prev, ...w }
    return prev
  }

  __resetForTests(): void {
    if (this.heartbeat !== null) clearInterval(this.heartbeat)
    this.heartbeat = null
    this.sessions.clear()
    this.anchors.clear()
    this.frameListeners.clear()
    this.notify()
  }
}

export const browserLiveStore = new BrowserLiveStore()

// ---- hooks ------------------------------------------------------------------

/** The record the card for a turn shows, or `null` for a turn with none. */
export function useBrowserLiveTurn(sessionKey: string | null, anchorKey: string | null, running: boolean, anchorTs: number | null): BrowserLiveTurn | null {
  return useSyncExternalStore(
    browserLiveStore.subscribe,
    () => (sessionKey && anchorKey ? browserLiveStore.resolveTurn(sessionKey, anchorKey, running, anchorTs) : null),
    () => null,
  )
}

/** True while the person holds the session's browser (the footer reads this). */
export function useBrowserLiveInControl(sessionKey: string | null): boolean {
  return useSyncExternalStore(
    browserLiveStore.subscribe,
    () => browserLiveStore.inControl(sessionKey),
    () => false,
  )
}

/** Whether `turnId` is the session's newest record (only that card may "Open live"). */
export function useBrowserLiveIsCurrent(sessionKey: string | null, turnId: string): boolean {
  return useSyncExternalStore(
    browserLiveStore.subscribe,
    () => Boolean(sessionKey && turnId) && browserLiveStore.isCurrentTurn(sessionKey as string, turnId),
    () => false,
  )
}

/** Every turn this page has seen for a session (the replay's turn picker). */
export function useBrowserLiveSessionTurns(sessionKey: string | null): readonly BrowserLiveTurn[] {
  return useSyncExternalStore(
    browserLiveStore.subscribe,
    () => browserLiveStore.sessionTurns(sessionKey),
    () => EMPTY_TURNS,
  )
}

/** The session's current turn record (the stage follows it when not pinned). */
export function useBrowserLiveCurrent(sessionKey: string | null): BrowserLiveTurn | null {
  return useSyncExternalStore(
    browserLiveStore.subscribe,
    () => browserLiveStore.current(sessionKey),
    () => null,
  )
}

/** One turn's record by id. */
export function useBrowserLiveTurnById(sessionKey: string | null, turnId: string | null): BrowserLiveTurn | null {
  return useSyncExternalStore(
    browserLiveStore.subscribe,
    () => browserLiveStore.turn(sessionKey, turnId),
    () => null,
  )
}
