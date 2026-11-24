/**
 * Point and talk: a finger that lands on a Bot makes it turn and listen, the
 * person speaks, and a pause ends the sentence. There is no pinch, no button
 * and no wake word, so three questions are answered here, with no clock,
 * scene or microphone of their own:
 *
 * 1. Is this hand pointing, or just moving? A hand points when its index
 *    finger is held out clearly further than the other three, it is not
 *    pinching, and both the hand and its ray are nearly still. An open hand,
 *    a fist, a pinch and a hand that sweeps past all fail one of these.
 * 2. Which Bot is meant, with two close together? The Bot nearest the ray,
 *    but only when it is clearly nearer than the next one. When the two are
 *    too close to call, neither listens, and both are named so the scene can
 *    say so. A Bot that already listens keeps the turn until the ray has
 *    clearly left it, so a small tremor never hands the sentence to its
 *    neighbour.
 * 3. When does the Bot listen, and when is the sentence over? The Bot is
 *    noticed the moment the finger lands, and sound is kept from then. It
 *    is the listener for good once the finger has rested a moment, or with
 *    the first word, whichever comes first. From then on the hand is free:
 *    it can go back to work, take the thing the sentence is about, or point
 *    at it. Only a pause after words ends the sentence (or the recogniser's
 *    word that it is complete): a short one after a whole sentence, a
 *    longer one while the sentence is still open. A dropped hand ends nothing, and a point at
 *    another Bot in the middle of a sentence does not change who listens.
 *    One point is one sentence: the finger must leave and come back before
 *    the same Bot listens again.
 *
 * What a finished sentence does is not decided here. The scene hands it to
 * the press route, which holds the same rules as every other send.
 */
import type { V3 } from './hand'
import { DEG } from './metrics'
import { TUNE } from './tune'

export const POINT = {
  /** How long the finger rests on a Bot before it listens. */
  REST_MS: 350,
  /** Silence after words that ends the sentence. */
  PAUSE_MS: TUNE.sayPauseMs,
  /** The same, when the words so far are not a whole sentence yet: the person may be thinking. */
  PAUSE_OPEN_MS: TUNE.sayPauseOpenMs,
  /** After the sentence, the Bot shows what it understood for this long before it starts. "Never mind" in it drops the sentence. */
  BEAT_MS: TUNE.sayBeatMs,
  /** A Bot that listens and hears nothing gives up after this. */
  WAIT_MS: 6000,
  /** A ray that turns faster, or a hand that moves faster, is a hand in motion (degrees and metres per second). */
  MAX_TURN: 40,
  MAX_MOVE: 0.35,
  /** The index finger must be this straight, and this much straighter than the other three. */
  INDEX_OUT: 0.8,
  GAP: 0.18,
  /** A Bot can be pointed at within this angle of its middle, however small it is drawn. */
  PICK_DEG: 2.5,
  /** The Bot that listens keeps the turn this much further out. */
  EXIT_DEG: 2,
  /** Two Bots nearer to each other than this, as seen from the ray, are too close to call. */
  SEP_DEG: 1.5,
} as const

export interface FingerShape { index: number; others: number }
export type Joints = Readonly<Record<string, V3 | null | undefined>>

const dist = (a: V3, b: V3): number => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])

/** How straight one finger is, 0..1: the tip's distance from the knuckle against the finger's own length. */
function straightness(j: Joints, finger: string): number | null {
  const names = ['phalanx-proximal', 'phalanx-intermediate', 'phalanx-distal', 'tip'].map(n => j[`${finger}-finger-${n}`])
  if (names.some(p => !p)) return null
  const [a, b, c, d] = names as V3[]
  const len = dist(a, b) + dist(b, c) + dist(c, d)
  return len > 1e-6 ? dist(a, d) / len : null
}

/** The shape of a hand from its joints, or null when the joints are not all known. */
export function fingerShape(j: Joints): FingerShape | null {
  const index = straightness(j, 'index')
  const rest = ['middle', 'ring', 'pinky'].map(f => straightness(j, f))
  if (index === null || rest.some(v => v === null)) return null
  return { index, others: (rest as number[]).reduce((a, b) => a + b, 0) / 3 }
}

export interface HandSample {
  t: number
  origin: V3
  dir: V3
  /** Null when the source has no fingers to read (the mouse of the desktop preview). */
  shape: FingerShape | null
  pinching: boolean
}

/** Tells a pointing hand from a hand that is just moving. One per hand. */
export class PointDetector {
  private last: HandSample | null = null
  private turn = 0
  private move = 0

  /** Feed the hand as it is now (null when it is not tracked). True while it points. */
  feed(s: HandSample | null): boolean {
    const prev = this.last
    this.last = s
    if (!s) { this.turn = 0; this.move = 0; return false }
    if (prev && s.t > prev.t) {
      const dt = (s.t - prev.t) / 1000
      const dot = Math.max(-1, Math.min(1, s.dir[0] * prev.dir[0] + s.dir[1] * prev.dir[1] + s.dir[2] * prev.dir[2]))
      // Smoothed over about a tenth of a second, so one noisy frame neither starts nor ends a point.
      const k = 1 - Math.exp(-dt / 0.1)
      this.turn += (Math.acos(dot) / DEG / dt - this.turn) * k
      this.move += (dist(s.origin, prev.origin) / dt - this.move) * k
    }
    if (s.pinching) return false
    if (s.shape && (s.shape.index < POINT.INDEX_OUT || s.shape.index - s.shape.others < POINT.GAP)) return false
    return this.turn < POINT.MAX_TURN && this.move < POINT.MAX_MOVE
  }

  /** How fast the ray turns and the hand moves right now, for the tests. */
  get speed(): { turn: number; move: number } { return { turn: this.turn, move: this.move } }
}

export interface Candidate {
  id: string
  /** The angle between the ray and the Bot's middle, in degrees. */
  deg: number
  /** How big the Bot looks from the ray's origin: the angle of its half height. */
  radius: number
}

/** The Bot a ray means. `current` is the Bot that already listens, or ''. */
export function chooseBot(cands: readonly Candidate[], current: string): { id: string; ambiguous: string[] } {
  const reach = (c: Candidate) => Math.max(c.radius, POINT.PICK_DEG) + (c.id === current ? POINT.EXIT_DEG : 0)
  const near = cands.filter(c => c.deg <= reach(c)).sort((a, b) => a.deg - b.deg)
  if (!near.length) return { id: '', ambiguous: [] }
  // The Bot that listens keeps the turn for as long as the ray is still on it.
  if (current && near.some(c => c.id === current)) return { id: current, ambiguous: [] }
  const [first, second] = near
  if (second && second.deg - first.deg < POINT.SEP_DEG) return { id: '', ambiguous: [first.id, second.id] }
  return { id: first.id, ambiguous: [] }
}

export type TalkPhase = 'idle' | 'noticed' | 'listening' | 'hearing'
export type TalkEnd = 'pause' | 'endpoint' | 'silence' | 'left' | 'never-mind'
export interface TalkEvent {
  type: 'notice' | 'listen' | 'send' | 'keep' | 'cancel'
  bot: string
  text: string
  why?: TalkEnd
}

export interface TalkInput {
  now: number
  /** The hand points (see `PointDetector`). */
  pointing: boolean
  /** The Bot the ray is on (see `chooseBot`), or ''. It counts as pointed at only while the hand points. */
  bot: string
}

/** One sentence to one Bot, from the finger landing to the words being handed on. */
export class TalkSession {
  phase: TalkPhase = 'idle'
  bot = ''
  text = ''
  private since = 0
  private lastWord = 0
  /** The Bot that just had its sentence: the finger must leave it before it listens again. */
  private spent = ''

  private end(type: TalkEvent['type'], why: TalkEnd): TalkEvent {
    const e: TalkEvent = { type, bot: this.bot, text: this.text.trim(), why }
    this.spent = this.bot
    this.phase = 'idle'
    this.bot = ''
    this.text = ''
    return e
  }

  /** Call every frame. Returns what just happened, or null. */
  point(i: TalkInput): TalkEvent | null {
    const on = i.pointing ? i.bot : ''
    if (this.spent && on !== this.spent) this.spent = ''
    if (this.phase === 'idle') {
      if (!on || on === this.spent) return null
      this.phase = 'noticed'
      this.bot = on
      this.since = i.now
      return { type: 'notice', bot: on, text: '' }
    }
    if (this.phase === 'noticed') {
      // The first word makes the Bot the listener at once. Nobody waits for the finger to have rested before they talk.
      if (!this.text) {
        if (on !== this.bot) { const e = this.end('cancel', 'left'); this.spent = ''; return e }
        if (i.now - this.since < POINT.REST_MS) return null
      }
      this.phase = this.text ? 'hearing' : 'listening'
      this.since = i.now
      return { type: 'listen', bot: this.bot, text: '' }
    }
    // The Bot listens, and the hand is free to go: only time ends this now.
    if (this.phase === 'listening') return i.now - this.since > POINT.WAIT_MS ? this.end('cancel', 'silence') : null
    return i.now - this.lastWord >= (wholeSentence(this.text) ? POINT.PAUSE_MS : POINT.PAUSE_OPEN_MS) ? this.end('send', 'pause') : null
  }

  /** The whole sentence so far, as the recogniser has it now. Words count from the moment the Bot was noticed. */
  heard(text: string, now: number): void {
    if (this.phase === 'idle' || !text.trim()) return
    if (text !== this.text) this.lastWord = now
    this.text = text
    if (this.phase === 'listening') this.phase = 'hearing'
  }

  /** The recogniser judged the sentence complete. */
  endpoint(): TalkEvent | null {
    return this.phase === 'hearing' ? this.end('send', 'endpoint') : null
  }

  /** Stop without sending (Escape, the session ending). Words that were heard are handed back to be kept. */
  stop(): TalkEvent | null {
    if (this.phase === 'idle') return null
    return this.end(this.text.trim() ? 'keep' : 'cancel', 'left')
  }

  /** "Never mind": the sentence is dropped, words and all. */
  drop(): TalkEvent | null {
    return this.phase === 'idle' ? null : this.end('cancel', 'never-mind')
  }
}

/** Whether the words so far read as a whole sentence: they end with a mark that ends one. A recogniser that gives no marks leaves every sentence open. */
export const wholeSentence = (text: string): boolean => /[.!?\u3002\uff01\uff1f\u0964]\s*$/.test(text)

/**
 * Whether what was heard ends with one of a few short phrases ("never mind", "stop"). The phrases come from the
 * language catalog, split by "|". They are meanings in ordinary words, matched whole at the end of what was said.
 */
export function endsWith(heard: string, phrases: string): boolean {
  const plain = (t: string) => t.toLocaleLowerCase().replace(/[\p{P}\p{S}]+/gu, ' ').replace(/\s+/g, ' ').trim()
  const said = ` ${plain(heard)}`
  return phrases.split('|').map(plain).filter(Boolean).some(ph => said.endsWith(` ${ph}`))
}

/** Whether one of a few short phrases is anywhere in what was heard. Whole words, like `endsWith`. */
export function hasPhrase(heard: string, phrases: string): boolean {
  const plain = (t: string) => t.toLocaleLowerCase().replace(/[\p{P}\p{S}]+/gu, ' ').replace(/\s+/g, ' ').trim()
  const said = ` ${plain(heard)} `
  return phrases.split('|').map(plain).filter(Boolean).some(ph => said.includes(` ${ph} `))
}

/** Whether what was heard begins with one of a few short phrases ("no"). Whole words, like `endsWith`. */
export function beginsWith(heard: string, phrases: string): boolean {
  const plain = (t: string) => t.toLocaleLowerCase().replace(/[\p{P}\p{S}]+/gu, ' ').replace(/\s+/g, ' ').trim()
  const said = `${plain(heard)} `
  return phrases.split('|').map(plain).filter(Boolean).some(ph => said.startsWith(`${ph} `))
}
