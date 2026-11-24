/**
 * A Bot's own thread was cut while the person is in: when its step comes, once, he is shown the cut. Since the failed
 * sheet (D35, P3 slice 2) that step brings no page in any view: the Bot holds the sheet up (`useSays.ts`). Plain
 * rules with no clock of their own and nothing drawn; `useAskPanels.ts` feeds them and takes the step.
 *
 * What brings a page is a cut that the reads see BEGIN: the Bot's own thread was not interrupted in the read
 * before and is in this one (`XrBot.cut`). The Bot's folded state never brings one: it turns to failed also when a
 * worker finishes over an old cut, or when he answers the Bot's ask. A worker's cut brings none: its Resume is in
 * the worker's own chat, which a panel cannot open. What was cut when the page opened, or when its live connection
 * came back (a gateway that restarted), keeps its red rim only.
 *
 * Many cuts at once (an agent backend that died under every running turn) bring one page at most: cuts of two Bots
 * or more in one read keep their rims only; a cut read alone brings its page at once; and while one cut's page
 * waits, or within a few seconds after the first cut whose page came up, another Bot's cut keeps its rim. That first
 * page stays: no panel comes and goes.
 *
 * What is new is told only from a whole read (`wholeRead`), as for the asks.
 */
import { wholeRead, type XrCrew } from '../model'

export const FAILS = {
  /** After he kept a failure's page or sent it away, its Bot brings none for so long. */
  quietMs: 10 * 60 * 1000,
  /** Another Bot's cut read within so long after the cut whose page came up keeps its rim only. */
  massMs: 5000,
} as const

/** What the XR page knows of the Bots' cut threads. */
export interface FailBook {
  /** A whole read was taken since the page opened or its live connection came back: from then on a cut is new. */
  started: boolean
  /** Each Bot's cut as of the last whole read ('' for none). */
  cuts: Map<string, string>
  /** The cuts that wait to come up, by Bot, with when each was first read. */
  waiting: Map<string, { cut: string; at: number }>
  /** Cuts that are done with: each brings a page at most once. */
  done: Set<string>
  /** The last cut whose page came up: its Bot, and when that cut was first read. */
  lastUp: { bot: string; at: number } | null
  /** Until when a Bot brings no failure's page. */
  quietUntil: Map<string, number>
}

export const newFailBook = (): FailBook => ({ started: false, cuts: new Map(), waiting: new Map(), done: new Set(), lastUp: null, quietUntil: new Map() })

/** Take a read of the crew into the book. A read while the live connection is down makes the next whole read count as the page's opening. */
export function readFails(book: FailBook, crew: XrCrew, now: number): void {
  if (!crew.feeds.live) { book.started = false; return }
  if (!wholeRead(crew.feeds)) return
  const cuts = new Map<string, string>()
  const fresh: { bot: string; cut: string }[] = []
  for (const bot of crew.bots) {
    const cut = bot.cut
    cuts.set(bot.slug, cut)
    if (!cut) { book.waiting.delete(bot.slug); continue }
    // A cut that is not the one that waits replaces it: the newest turn is the one to resume.
    if (book.waiting.has(bot.slug) && book.waiting.get(bot.slug)?.cut !== cut) book.waiting.delete(bot.slug)
    if (cut === (book.cuts.get(bot.slug) ?? '') || book.done.has(cut)) continue
    if (!book.started) { book.done.add(cut); continue }
    fresh.push({ bot: bot.slug, cut })
  }
  // One page at most: two Bots' cuts in one read, a cut while another Bot's waits, or one read soon after the cut
  // whose page came up, keep their rims only.
  const soonAfter = book.lastUp !== null && now - book.lastUp.at < FAILS.massMs
  for (const f of fresh) {
    const other = [...book.waiting.keys()].some(b => b !== f.bot) || (soonAfter && book.lastUp?.bot !== f.bot)
    if (fresh.length >= 2 || other) book.done.add(f.cut)
    else book.waiting.set(f.bot, { cut: f.cut, at: now })
  }
  book.cuts = cuts
  book.started = true
}

/** A Bot's cut is done with: its page came up, its standing page was lit, or it brings none. */
export function failDone(book: FailBook, bot: string): void {
  const cut = book.waiting.get(bot)?.cut
  if (cut) book.done.add(cut)
  book.waiting.delete(bot)
}

/** A Bot's cut brought its page up: done with, and the first of the few seconds in which no other cut brings one. */
export function failUp(book: FailBook, bot: string): void {
  const w = book.waiting.get(bot)
  if (w) book.lastUp = { bot, at: w.at }
  failDone(book, bot)
}

/**
 * What a whole read makes of a failure's page that he has not pressed. Its Bot's cut is still its cut: it stays
 * (a short drop of the live connection closes nothing). Its Bot's cut is of another gateway process (another
 * `turn_gen`): the gateway restarted, and it closes with that line. Else its turn ran again, and it closes.
 */
export function failGone(pageCut: string, botCut: string): 'stays' | 'restart' | 'ran' {
  if (botCut === pageCut) return 'stays'
  const gen = (cut: string) => cut.slice(cut.lastIndexOf('#') + 1, cut.lastIndexOf(':'))
  return botCut !== '' && gen(botCut) !== gen(pageCut) ? 'restart' : 'ran'
}

/** He kept a failure's page or sent it away: that Bot brings none for a while. */
export const failQuiet = (book: FailBook, bot: string, now: number): void => { book.quietUntil.set(bot, now + FAILS.quietMs) }

export interface FailMoment {
  now: number
  /** The gateway's panels are open, and nothing stopped them. */
  open: boolean
  /** Since when the person has not been busy. `null`: busy now. */
  calmSince: number | null
  calmMs: number
  /** A Bot asks now: its ask's panel is that Bot's page. */
  asks: (bot: string) => boolean
  /** The panel that shows a Bot's page now, his or an ask's, or `null`. */
  standing: (bot: string) => string | null
  /** An ask's or a failure's panel stands in view. */
  panelInView: boolean
  /** An ask waits to come up: asks go first. */
  askWaits: boolean
  /** How many pages are open, and how many the gateway holds. */
  pages: number
  most: number
}

export type FailStep = { do: 'nothing' } | { do: 'up'; bot: string } | { do: 'light'; bot: string; id: string }

/**
 * What to do now for the cuts that wait. A Bot that asks, or that is quiet after he kept or sent away its failure's
 * page, brings none. A Bot whose page stands has its frame lit once. Else its page comes up at once, when no ask
 * waits, no ask's or failure's panel stands in view and the person is calm. A full room brings none.
 */
export function nextFail(book: FailBook, at: FailMoment): FailStep {
  if (!book.started || !at.open) return { do: 'nothing' }
  for (const bot of [...book.waiting.keys()]) {
    if ((book.quietUntil.get(bot) ?? 0) > at.now || at.asks(bot)) { failDone(book, bot); continue }
    const id = at.standing(bot)
    if (id) { failDone(book, bot); return { do: 'light', bot, id } }
    if (at.pages >= at.most) { failDone(book, bot); continue }
    if (at.askWaits || at.panelInView) return { do: 'nothing' }
    if (at.calmSince === null || at.now - at.calmSince < at.calmMs) return { do: 'nothing' }
    return { do: 'up', bot }
  }
  return { do: 'nothing' }
}
