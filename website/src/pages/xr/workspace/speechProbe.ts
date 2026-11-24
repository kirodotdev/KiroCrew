/**
 * A probe of the browser's own speech recognition (the Web Speech API), for one question: does it run while an
 * immersive session runs, and does the session stay? It starts and stops one recognition, keeps what was heard and
 * how the session fared meanwhile, and says it as one line for the readout (`?fps=1&speech=1`).
 *
 * The browser's recognition can send the sound to its maker's servers; the person allowed that (the line and the
 * tile say plainly when the microphone is on). Nothing here sends sound or words anywhere itself, and nothing here
 * acts on what was heard. It is its own file so that a part that takes speech for real can use it.
 */
import { i18nT } from '../../../i18n/t'
import { fmtNumber } from '../../../i18n/format'

/** The part of a browser's recognition that the probe uses. */
export interface Recognition {
  lang: string
  continuous: boolean
  interimResults: boolean
  onstart: (() => void) | null
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null
  onspeechend: (() => void) | null
  onerror: ((e: { error?: string }) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
  abort(): void
}

/**
 * `none`: this browser has no recognition. `off`: nothing was asked yet. `asking`: a recognition was started and the
 * browser has not said that it listens (it may be asking the person for the microphone). `on`: the microphone is on.
 * `heard`: it ended with words, or with none. `error`: it ended with the browser's error.
 */
export type SpeechPhase = 'none' | 'off' | 'asking' | 'on' | 'heard' | 'error'

/**
 * How the immersive session fared from the moment a recognition was asked for: `null` when none ran then.
 * `blurred`: the session went on but something stood over it and held its input (WebXR's `visible-blurred`, as
 * under a permission prompt). `hidden`: its picture was gone. The worst of them stays said (`SESSION_RANK`).
 */
export type SessionFared = 'stayed' | 'blurred' | 'hidden' | 'ended' | null

/** How a session is in one frame, as the probe is told. */
export type SessionNow = 'visible' | 'blurred' | 'hidden' | 'ended'

/** `ended` over `hidden` over `blurred` over `stayed`. */
const SESSION_RANK = { stayed: 0, blurred: 1, hidden: 2, ended: 3 } as const

/** A session's `visibilityState` (`null`: there is no session), as the probe takes it. */
export function sessionNow(state: string | null | undefined): SessionNow {
  if (state === null) return 'ended'
  return state === 'hidden' ? 'hidden' : state === 'visible-blurred' ? 'blurred' : 'visible'
}

export interface SpeechState {
  phase: SpeechPhase
  /** The language asked for, as a tag (`en-US`). */
  lang: string
  /** The words heard so far, or at the end. */
  text: string
  /** From the end of the speech to the last words, in ms; `null` when the browser gave no end of speech before them. */
  afterMs: number | null
  /**
   * The browser's own word for what went wrong (`not-allowed`, `no-speech`, `network`); `no-answer` when it never
   * said that it began and was given up; `no-end` when it was told to stop, never said that it ended, and was given up.
   */
  error: string
  session: SessionFared
}

/** The browser's recognition, or `null` where it has none. */
export function browserRecognition(): (() => Recognition) | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition }
  const Made = w.SpeechRecognition ?? w.webkitSpeechRecognition
  return Made ? () => new Made() : null
}

export class SpeechProbe {
  state: SpeechState
  /** Told at every change, so that what draws the state can draw it anew. */
  changed: (() => void) | null = null
  private running: Recognition | null = null
  private speechEndAt: number | null = null
  /** The running recognition was told to stop, and has not said that it ended. */
  private stopping = false

  constructor(private readonly make: (() => Recognition) | null = browserRecognition(), private readonly now: () => number = () => performance.now()) {
    this.state = { phase: make ? 'off' : 'none', lang: '', text: '', afterMs: null, error: '', session: null }
  }

  /** Whether a recognition runs: asked for, or listening. */
  get busy(): boolean { return this.state.phase === 'asking' || this.state.phase === 'on' }

  private set(next: Partial<SpeechState>): void {
    this.state = { ...this.state, ...next }
    this.changed?.()
  }

  /** Start listening in `lang`; while one runs, stop it instead. `inSession`: an immersive session runs now. */
  listen(lang: string, inSession: boolean): void {
    if (!this.make) return
    if (this.busy) { this.stop(); return }
    const rec = this.make()
    rec.lang = lang
    rec.continuous = false
    rec.interimResults = true
    this.running = rec
    this.speechEndAt = null
    this.stopping = false
    this.set({ phase: 'asking', lang, text: '', afterMs: null, error: '', session: inSession ? 'stayed' : null })
    // Every answer is for the recognition that is running: one that was replaced says nothing any more.
    const mine = () => this.running === rec
    rec.onstart = () => { if (mine()) this.set({ phase: 'on' }) }
    rec.onspeechend = () => { if (mine()) this.speechEndAt = this.now() }
    rec.onresult = e => {
      if (!mine()) return
      let text = ''
      for (let i = 0; i < e.results.length; i += 1) text += e.results[i][0]?.transcript ?? ''
      this.set({ text: text.trim(), afterMs: this.speechEndAt === null ? null : Math.max(0, this.now() - this.speechEndAt) })
    }
    rec.onerror = e => { if (mine()) { this.running = null; this.set({ phase: 'error', error: e.error || 'unknown' }) } }
    rec.onend = () => { if (mine()) { this.running = null; this.set({ phase: 'heard' }) } }
    try { rec.start() } catch (e) { this.running = null; this.set({ phase: 'error', error: e instanceof Error ? e.name : 'unknown' }) }
  }

  /**
   * Stop listening: the browser ends the recognition with what it has. A browser that stays silent does not hold
   * the probe: a recognition that it never said it began is given up instead (`no-answer`), and so is one that was
   * told to stop before and has not ended (`no-end`). Giving up aborts it, which is the most a page can do.
   */
  stop(): void {
    const rec = this.running
    if (!rec) return
    const on = this.state.phase === 'on'
    if (on && !this.stopping) { this.stopping = true; rec.stop(); return }
    this.running = null
    rec.abort()
    this.set({ phase: 'error', error: on ? 'no-end' : 'no-answer' })
  }

  /**
   * How the session is now, told each frame while a recognition runs: once it was blurred, hidden or ended, the
   * worst of them stays said, since that is what the probe is for. A session that ends has given the probe its
   * answer: the recognition is stopped.
   */
  sessionIs(now: SessionNow): void {
    const was = this.state.session
    if (!this.busy || was === null || now === 'visible' || SESSION_RANK[now] <= SESSION_RANK[was]) return
    this.set({ session: now })
    if (now === 'ended') this.stop()
  }

  /** The part that drew the probe is gone: the microphone goes off with it. */
  dispose(): void {
    const rec = this.running
    this.running = null
    rec?.abort()
    if (this.busy) this.set({ phase: 'off' })
  }
}

const SESSION_KEY = { stayed: 'xr.readout_session_stayed', blurred: 'xr.readout_session_blurred', hidden: 'xr.readout_session_hidden', ended: 'xr.readout_session_ended' } as const

/** The `speech` line of the readout. It says in capitals when the microphone is on. */
export function speechLine(s: SpeechState): string {
  const session = i18nT(s.session ? SESSION_KEY[s.session] : 'xr.readout_session_none')
  if (s.phase === 'none') return i18nT('xr.readout_speech_none')
  if (s.phase === 'off') return i18nT('xr.readout_speech_off')
  if (s.phase === 'asking') return i18nT('xr.readout_speech_asking', { lang: s.lang, glasses: session })
  if (s.phase === 'on') return i18nT('xr.readout_speech_on', { lang: s.lang, text: s.text || '-', glasses: session })
  if (s.phase === 'error') return i18nT('xr.readout_speech_error', { lang: s.lang, error: s.error, glasses: session })
  return i18nT('xr.readout_speech_heard', { lang: s.lang, text: s.text || '-', ms: s.afterMs === null ? '-' : fmtNumber(s.afterMs, { maximumFractionDigits: 0 }), glasses: session })
}

/** The one probe of the XR page. */
export const speechProbe = new SpeechProbe()
