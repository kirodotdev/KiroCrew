/**
 * What a Bot's rim says of its cut sessions (its own thread and its workers): red for a cut that he has not seen,
 * a small dim mark for one that he has. Plain rules with no clock of their own and nothing drawn; `Workspace.tsx`
 * feeds them and `Crew.tsx` draws what comes out.
 *
 * Each word of the rule is a fact that the XR page reads:
 * - a cut: the slot says `interrupted` (`XrBot.cuts`);
 * - unread: the gateway's read state has the slot's key in `dashboard.unreadSlots` (`XrCut.unread`);
 * - it began while he is in: the slot was in the last whole read, not cut or cut with another key, and is cut in
 *   this one, after the page's opening or its reconnect (as the cut page's book reads it, `fails.ts`, for the
 *   workers too). A slot that enters the list already cut (a worker's chat reopened on his Mac) did not begin;
 * - he pressed that Bot: a press in XR on its seat, its place in the row, or a panel that shows its page, after the
 *   cut was read.
 *
 * RED: a session is cut, he has not pressed that Bot since that cut, and the gateway calls the session unread or
 * the page saw the cut begin. Any other cut shows the dim mark. A Bot's own thread turns unread only when the Bot
 * spoke, so a cut of it from while he was away is dim; a worker's is red until he reads it or presses that Bot.
 * The press and what began are lost at a reload; then the read state alone decides.
 */
import { wholeRead, type XrBot, type XrCrew } from '../model'

/** What the XR page knows of the cuts it read. */
export interface RimBook {
  /** A whole read was taken since the page opened or its live connection came back: from then on a cut is new. */
  started: boolean
  /** Every session of the last whole read. */
  sessions: Set<string>
  /** Each cut session's cut as of the last whole read; a session of `sessions` that is not here was not cut. */
  cuts: Map<string, string>
  /** The cuts that began while he was in. */
  begun: Set<string>
}

export const newRimBook = (): RimBook => ({ started: false, sessions: new Set(), cuts: new Map(), begun: new Set() })

/** Take a read of the crew into the book. A read while the live connection is down makes the next whole read count as the page's opening. */
export function readRims(book: RimBook, crew: XrCrew): void {
  if (!crew.feeds.live) { book.started = false; return }
  if (!wholeRead(crew.feeds)) return
  const cuts = new Map<string, string>()
  for (const bot of crew.bots) for (const c of bot.cuts) {
    cuts.set(c.slot, c.id)
    if (book.started && book.sessions.has(c.slot) && book.cuts.get(c.slot) !== c.id) book.begun.add(c.id)
  }
  book.sessions = new Set(crew.bots.flatMap(b => b.sessions))
  book.cuts = cuts
  book.started = true
}

/**
 * Whether a press on a panel that shows a Bot's page is a press on that Bot: when the press gate sends it to the page
 * or it keeps the panel (`asks.pressFate`), not when the gate holds it back (a pinch aimed at a panel that had just
 * gone lands on the one under it). A page that the tab draws has no such gate: its own press counts at once.
 */
export const pressCounts = (fate: 'send' | 'hold' | 'keep'): boolean => fate !== 'hold'

/** The cuts that a press on this Bot makes seen: every cut of it that was read. */
export const pressedCuts = (bot: Pick<XrBot, 'cuts'>): string[] => bot.cuts.map(c => c.id)

/**
 * The Bot as its rim draws it: `failed` only for a cut that he has not seen and the gateway does not call old
 * (`stale`), and `dim` for the other cuts.
 */
export function rimOf(bot: XrBot, book: RimBook, pressed: ReadonlySet<string>): XrBot {
  if (!bot.cuts.length) return bot
  // Each cut says whether it is red: a failed sheet comes for a red one only (P3 slice 2).
  const cuts = bot.cuts.map(c => ({ ...c, red: !c.stale && !pressed.has(c.id) && (c.unread || book.begun.has(c.id)) }))
  const red = cuts.some(c => c.red)
  // Needs and working go first: a red cut under them shows nothing until they end, and a seen one its mark.
  if (bot.state !== 'failed') return red ? { ...bot, cuts } : { ...bot, cuts, dim: true }
  return red ? { ...bot, cuts } : { ...bot, cuts, state: 'idle', dim: true }
}
