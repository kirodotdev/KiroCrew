/**
 * How a person answers a request that a Bot holds out, as one small interface.
 *
 * The route that acts on an answer knows two words only, yes and no: `scene.ts` makes one guarded press per
 * answer, and `router.ts` checks it. Which movement of a hand or which spoken word MEANS yes or no is decided
 * here, by a list of ways. Swapping the way of saying yes is a change to `WAYS`; the route is not touched.
 *
 * The person uses hands only unless the land is opened with voice (`?view=land&voice=1`). With hands only, `HANDS` is the
 * list in use: the hand-back is the yes, and the bin is the no. With voice, `WAYS` is.
 *
 * With voice, the main one first:
 *   point and say   The finger is on the card, or a hand holds it, and the person says yes in any words.
 *                   Both together. It takes one finger, so the other hand keeps what it holds.
 *                   A no in words needs no finger: it is said to the Bot that holds the card out.
 *   hand back       The card ends in its Bot's hand and is let go: yes without speaking. Thrown, or let go over
 *                   the bin: no.
 *
 * What "on the card" and "in the Bot's hand" mean is measured, and the numbers are in `tune.ts`:
 * `onCardForWord`, `tipOnCard` and `whereLet` below. After any yes the Bot reads the command aloud and waits one
 * beat, and `stopsYes` says which words stop it.
 *
 * A known risk, kept on purpose: a person who holds the card and says "yeah" to someone else in the room has said
 * yes. Only the beat after the yes catches it, and only if the person notices.
 *
 * Parked, not in `WAYS`:
 *   turn a key      `KeyHold` in `gestures.ts`. Nothing feeds a moment its `turn` today.
 *
 * Never a yes, in any way in use: a word with no finger or hand on the card, a card that is only held, pointed
 * at or looked at, and a card that is put down anywhere but on its Bot.
 */
import type { KeyEnd } from './gestures'
import type { ThrowKind } from './handling'
import { beginsWith, hasPhrase } from './pointTalk'
import { TUNE } from './tune'

export type Answer = 'yes' | 'no' | 'nothing'

/** Where a card that a hand carried was let go: on its Bot, in the bin, or anywhere else. '' when no card was let go. */
export type LetGo = '' | 'bot' | 'bin' | 'away'

/** One moment that could be an answer: a sentence that just ended, or a card that was just let go. */
export interface AskMoment {
  /** The finger was on the card, or a hand held it, as the first word of `words` was said (`onCardForWord`). */
  onCard: boolean
  /** A whole sentence said to the Bot that holds the card out, or ''. */
  words: string
  letGo: LetGo
  /** Parked: how a hold of the key ended. */
  turn?: KeyEnd
}

/** The spoken phrases that mean yes and no, from the language catalog, split by "|". */
export interface AnswerWords { yesWords: string; noWords: string }

export interface AnswerWay {
  readonly id: string
  read(m: AskMoment, w: AnswerWords): Answer
}

export const pointAndSay: AnswerWay = {
  id: 'point-and-say',
  read(m, w) {
    if (!m.words) return 'nothing'
    if (beginsWith(m.words, w.noWords)) return 'no'
    // "Ok, no, wait" is not a yes: a sentence with a no in it never allows.
    return m.onCard && beginsWith(m.words, w.yesWords) && !hasPhrase(m.words, w.noWords) ? 'yes' : 'nothing'
  },
}

export const handBack: AnswerWay = {
  id: 'hand-back',
  read: m => (m.letGo === 'bot' ? 'yes' : m.letGo === 'bin' ? 'no' : 'nothing'),
}

/** Parked. A key turned to its click allows, and one let go out of its lock refuses. */
export const turnKey: AnswerWay = {
  id: 'turn-key',
  read: m => (m.turn === 'turned' ? 'yes' : m.turn === 'pulled' ? 'no' : 'nothing'),
}

/** The ways in use with voice on: the spoken yes first, then the hand-back. */
export const WAYS: readonly AnswerWay[] = [pointAndSay, handBack]

/** The ways in use with hands only, which is how the page runs unless voice is asked for. The spoken yes is parked, not gone. */
export const HANDS: readonly AnswerWay[] = [handBack]

/** What a moment means. A no from any way wins over a yes from another. */
export function answerOf(m: AskMoment, w: AnswerWords, ways: readonly AnswerWay[] = WAYS): Answer {
  const said = ways.map(way => way.read(m, w))
  return said.includes('no') ? 'no' : said.includes('yes') ? 'yes' : 'nothing'
}

/** How a carried card was let go: over the bin or not, how far from its Bot (null with no Bot), how near counts, and how fast the card moved. */
export interface LetGoAt { bin: boolean; botM: number | null; reachM: number; speedMps: number }

/**
 * Where a carried card was let go. It is in its Bot's hand only when it ended there: within reach of the Bot, and at
 * rest. A card that only moved toward the Bot is put down. A throw always lands in the bin, wherever it was aimed:
 * a flick is never a yes.
 */
export function whereLet(thrown: ThrowKind, at: LetGoAt): LetGo {
  if (thrown === 'throw' || at.bin) return 'bin'
  return at.botM !== null && at.botM <= at.reachM && at.speedMps < TUNE.cardStillMps ? 'bot' : 'away'
}

/** What the finger, or the hand that holds the card, did around the first word of a sentence. Times in ms. */
export interface WordOnCard {
  /** When the first word started. */
  wordAt: number
  /** Since when the card has rested where its Bot holds it out (0: it has not come to rest). */
  restAt: number
  /** Since when the finger was on the card, without a break, as the word started (0: it was not on it). */
  onSince: number
  /** Until when it stayed on it, without a break. */
  onUntil: number
  /** The fastest the finger moved from the word's start until `cardStayMs` later, in metres a second. */
  fastest: number
}

/**
 * Whether a word was said with the finger on the card. The card had rested a moment, so a word that was already
 * being said when the Bot arrived does not count. The finger was there as the word started, still there a moment
 * later, and slow, so a hand that passes through the card does not count.
 */
export function onCardForWord(w: WordOnCard): boolean {
  if (!(w.restAt > 0) || w.wordAt - w.restAt < TUNE.cardRestMs) return false
  if (!(w.onSince > 0) || w.onSince > w.wordAt) return false
  return w.onUntil - w.wordAt >= TUNE.cardStayMs && w.fastest < TUNE.cardStillMps
}

/**
 * Whether a fingertip is on the card. `tip` is the tip in the card's own space: x across and y up from its middle, z
 * off its face, in the card's own units. `scale` is how big the card is drawn.
 */
export function tipOnCard(tip: readonly [number, number, number], width: number, height: number, scale: number): boolean {
  return Math.abs(tip[0]) <= width / 2 && Math.abs(tip[1]) <= height / 2 && Math.abs(tip[2]) * scale <= TUNE.cardTouchM
}

/** Whether what was heard in the beat after a yes stops it: a no or a "wait", anywhere in it. */
export function stopsYes(heard: string, w: AnswerWords & { waitWords: string }): boolean {
  return hasPhrase(heard, `${w.noWords}|${w.waitWords}`)
}
