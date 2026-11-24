/**
 * Speech to text for the new chat card (`chatCard.ts`): the browser's own recognizer (the Web Speech API), behind
 * a small contract. Chrome's recognizer may send the sound to its maker's servers, so it runs only while a card is
 * out and says on the card that the microphone is on. Where the browser has none, or it refuses (no microphone, no
 * leave, no service, or it never starts), `none()` says so and the card goes to the keyboard.
 *
 * It does not listen without end: after `QUIET_MS` with no words it is stopped and `quiet()` says so, so that a
 * card left standing does not send the room's sound on. The language is the card's own (`speechLangs`): the
 * dashboard's at first, switched by a mark on the card, and the choice is kept in this browser. A language that
 * the recognizer does not know is not "no recognition here": `noLang()` says so, and the card goes to the first
 * language of its list (`langAfterRefusal`).
 */
import { SUPPORTED_LANGUAGES } from '../../../i18n/languages'

/** What of the browser's recognizer is used. */
interface Recognizer {
  lang: string
  continuous: boolean
  interimResults: boolean
  onstart: (() => void) | null
  onresult: ((e: { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null
  onerror: ((e: { error: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
  abort: () => void
}
type RecognizerClass = new () => Recognizer

const classOf = (w: unknown): RecognizerClass | null => {
  const named = w as { SpeechRecognition?: RecognizerClass; webkitSpeechRecognition?: RecognizerClass }
  return named.SpeechRecognition ?? named.webkitSpeechRecognition ?? null
}

/** Whether this browser has a speech recognizer at all. */
export const speechHere = (w: unknown = window): boolean => classOf(w) !== null

/** The errors after which listening again is of no use: there is no recognition here. */
const NONE = new Set(['not-allowed', 'service-not-allowed', 'audio-capture', 'network'])
/** The error for a language that the recognizer does not know: there is recognition here, in another language. */
const NO_LANG = 'language-not-supported'
/** A recognizer that ends this soon after its start, this many times in a row, does not work here. */
const SHORT_MS = 1000
const SHORT_MOST = 3
/** A recognizer that has not said that it listens this long after it was started does not work here (ms): time enough for a person to answer the browser's question about the microphone. */
export const START_MS = 8000
/** With no words for this long the recognizer is stopped (ms). The time counts from when it first listens, and anew from each word. */
export const QUIET_MS = 20_000

/** A dashboard language as a recognizer's language: every Chinese is `zh-CN` and every English `en-US`; another is itself; '' for what is no language tag. */
export function speechTag(locale: string): string {
  const tag = locale.trim()
  if (/^zh(-|$)/i.test(tag)) return 'zh-CN'
  if (/^en(-|$)/i.test(tag)) return 'en-US'
  return /^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$/.test(tag) ? tag : ''
}

/** The languages a card may listen in, as the recognizer names them: Chinese and English, and the dashboard's own when it is another. */
export function speechLangs(dashboard: string): string[] {
  const own = speechTag(dashboard)
  const first = ['zh-CN', 'en-US']
  return own && !first.includes(own) ? [...first, own] : first
}

/** The language a new card starts in: the one kept in this browser when it is one of the list, else the dashboard's, else the first. */
export function startLang(kept: string | null, dashboard: string): string {
  const list = speechLangs(dashboard)
  if (kept && list.includes(kept)) return kept
  const own = speechTag(dashboard)
  return list.includes(own) ? own : list[0]
}

/** The language after a pinch on the mark: the next of the list, and after the last the first. */
export const nextLang = (now: string, list: readonly string[]): string => list[(list.indexOf(now) + 1) % list.length] ?? now

/**
 * The language to listen in after the recognizer refused `now` as one it does not know: the first of the list, so
 * that the card still listens and still shows its mark. `null` when it was the first that was refused: then the
 * microphone is turned off, and the mark stays for another language.
 */
export const langAfterRefusal = (now: string, list: readonly string[]): string | null => (list.length && list[0] !== now ? list[0] : null)

/** A language's own name for itself, as the mark shows it. */
export function langName(tag: string): string {
  if (tag === 'zh-CN') return '\u4E2D\u6587'
  // English by the dashboard's own language list, as its language picker names it.
  if (tag === 'en-US') return SUPPORTED_LANGUAGES.find(l => l.code === 'en')?.label ?? tag
  try {
    const base = tag.split('-')[0]
    const name = new Intl.DisplayNames([tag], { type: 'language' }).of(base)
    return name && name !== base ? name : tag
  } catch { return tag }
}

/** Where the chosen language is kept in this browser. */
export const LANG_KEY = 'kirocrew:xr-card-lang'

export interface Heard {
  /** The microphone is on. */
  started: () => void
  /** Words that are final, and the words still worked on. */
  heard: (final: string, interim: string) => void
  /** There is no recognition here; nothing more comes. */
  none: () => void
  /** No words came for `QUIET_MS`: the recognizer is stopped; nothing more comes. */
  quiet?: () => void
  /** The recognizer does not know this language: it is stopped; nothing more comes. Without it, `none()` is said. */
  noLang?: () => void
}

/**
 * Listens until `stop()`, or until no words came for `QUIET_MS`. The recognizer ends by itself after a silence: it
 * is started again, so the card listens through a pause. Returns `null` where the browser has no recognizer.
 */
export function listen(lang: string, on: Heard, w: unknown = window, now: () => number = () => performance.now()): { stop: () => void } | null {
  const Class = classOf(w)
  if (!Class) return null
  let wanted = true
  let short = 0
  let startedAt = 0
  let rec: Recognizer | null = null
  let waits: ReturnType<typeof setTimeout> | null = null
  let hush: ReturnType<typeof setTimeout> | null = null
  const settle = () => { if (waits !== null) clearTimeout(waits); waits = null }
  const still = () => { if (hush !== null) clearTimeout(hush); hush = null }
  const giveUp = (say: () => void = on.none) => {
    settle()
    still()
    if (!wanted) return
    wanted = false
    try { rec?.abort() } catch { /* it had ended */ }
    say()
  }
  const quiet = () => {
    settle()
    still()
    if (!wanted) return
    wanted = false
    try { rec?.stop() } catch { /* it had ended */ }
    on.quiet?.()
  }
  // The quiet time starts when the recognizer first listens and anew at each word. A restart after the
  // recognizer's own end does not start it anew: a silent room is silent across restarts.
  const heardNow = () => { still(); if (wanted) hush = setTimeout(quiet, QUIET_MS) }
  const start = () => {
    try {
      const r = new Class()
      r.lang = lang
      r.continuous = true
      r.interimResults = true
      r.onstart = () => {
        settle()
        if (!wanted) return
        if (hush === null) heardNow()
        on.started()
      }
      r.onresult = e => {
        if (!wanted) return
        let final = ''
        let interim = ''
        for (let i = e.resultIndex; i < e.results.length; i += 1) {
          const words = e.results[i][0]?.transcript ?? ''
          if (e.results[i].isFinal) final += words
          else interim += words
        }
        if ((final + interim).trim()) heardNow()
        on.heard(final, interim)
      }
      r.onerror = e => {
        if (e.error === NO_LANG) giveUp(on.noLang ?? on.none)
        else if (NONE.has(e.error)) giveUp()
      }
      r.onend = () => {
        settle()
        if (!wanted) return
        short = now() - startedAt < SHORT_MS ? short + 1 : 0
        if (short >= SHORT_MOST) giveUp()
        else start()
      }
      startedAt = now()
      rec = r
      r.start()
      waits = setTimeout(() => giveUp(), START_MS)
    } catch { giveUp() }
  }
  start()
  return { stop: () => { settle(); still(); wanted = false; try { rec?.stop() } catch { /* it had ended */ } } }
}
