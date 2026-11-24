/**
 * What a Bot can be asked to do about a thing, with hands only.
 *
 * A thing is dragged onto a Bot (or thrown to it once it is lit), and the Bot holds out a few cards. Pinching one
 * is the whole sentence: the card's own words go to the Bot with the thing's title. Hands cannot say a new thing,
 * so the cards are a short fixed list, most useful first, chosen from what is known about the thing.
 *
 * How the cards are laid out is measured (`tune.ts`): they never overlap; two or three stand in one row; four to
 * eight in rows of up to four; past eight the last card is "more", which turns to the next cards.
 */
import { TUNE } from './tune'

export interface Choice {
  id: 'take' | 'unblock' | 'status' | 'summarise' | 'more'
  /** What the card says. For every card but "more" it is also what is sent. */
  label: string
}

/** The cards' own words, from the language catalog. */
export interface ChoiceWords { take: string; unblock: string; status: string; summarise: string; more: string }

/** What is known about the thing: which Bot it belongs to ('' when unknown), and how it stands. */
export interface ChoiceThing { owner: string; state: string }

/** The cards a Bot holds out for a thing, most useful first. */
export function choicesFor(thing: ChoiceThing, bot: string, w: ChoiceWords): Choice[] {
  const out: Choice[] = []
  // Work that belongs to another Bot can be handed over. Work that waits or failed can be unblocked.
  if (thing.owner && thing.owner !== bot) out.push({ id: 'take', label: w.take })
  if (thing.state === 'needs' || thing.state === 'failed') out.push({ id: 'unblock', label: w.unblock })
  out.push({ id: 'status', label: w.status }, { id: 'summarise', label: w.summarise })
  return out
}

export interface Fan {
  /** The cards on show, in reading order. The last one may be "more". */
  cards: Choice[]
  /** How many stand in each row. */
  rows: number[]
  /** How many turns of "more" there are in all. */
  pages: number
}

/** The cards of one turn, and how they stand. `page` wraps, so "more" on the last turn shows the first cards again. */
export function fan(all: readonly Choice[], page: number, w: ChoiceWords): Fan {
  const more: Choice = { id: 'more', label: w.more }
  const whole = all.length <= TUNE.choiceMax
  const per = whole ? Math.max(1, all.length) : TUNE.choiceMax - 1
  const pages = Math.max(1, Math.ceil(all.length / per))
  const at = ((page % pages) + pages) % pages
  const cards = whole ? [...all] : [...all.slice(at * per, at * per + per), more]
  return { cards, rows: rowsOf(cards.length), pages }
}

/** How `n` cards stand: up to three in one row, more in rows of up to four that are as even as they can be. */
export function rowsOf(n: number): number[] {
  if (n <= 3) return n > 0 ? [n] : []
  const rows = Math.ceil(n / TUNE.choiceRowMax)
  const out: number[] = []
  for (let i = 0, left = n; i < rows; i += 1) { const take = Math.ceil(left / (rows - i)); out.push(take); left -= take }
  return out
}
