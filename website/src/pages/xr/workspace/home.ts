/**
 * The Home surface of the room of pages, as plain rules: the four numbers that it shows, the short lines of what
 * waits for the person, where it stands and how big it is. Nothing here draws or reads a store: `useHome.ts` keeps
 * its state and `Home.tsx` draws it.
 *
 * Home is the room's main panel: numbers, large, and no long text. It stands straight ahead, where a person looks
 * first. While no page shows it is `large`, level with the eyes. While a page shows, that page has the place
 * straight ahead, and Home is `compact`: one strip in front of the uppermost rows of the first page's place, 16
 * degrees up, inside what a pair of glasses shows of a level look (20 degrees up) and inside the flat preview's start
 * picture. Those rows are the dashboard's own top bar: of the middle of the view it is the one band that holds
 * nothing he reads. The band at the page's lower edge is where a Bot holds its board up, and where a chat page has
 * its message box.
 *
 * It stands upright with a desk too (`desk=1`): a number that lies on a desk is read at a slant and under the
 * hands; the desk is for pressing, the space over it for reading.
 */
import type { XrBot, XrNeed } from '../model'
import { M_PER_PX, PAGES } from './layout'

/** How high the strip's small words are ("working", "need you"): no desk line's words are smaller to his eye (D39). */
export const STRIP_LABEL_M = 0.03

/**
 * The rows "done today" takes on Home (D35): its head while anything was filed or waits to be read, and a row a filed
 * line. Home and the since line over it lay Home out from the same rows (`homeLayout`'s `done`), or the line stands
 * inside large Home.
 */
export const homeDoneRows = (h: { done: readonly unknown[]; toRead: number }): { head: boolean; lines: number } => ({ head: h.done.length > 0 || h.toRead > 0, lines: h.done.length })

export type HomeForm = 'large' | 'compact'

/** The four numbers. */
export interface HomeStats { working: number; waiting: number; failed: number; done: number }

/**
 * Where a press on a line goes, so that it opens the ask that the line names:
 * - `bot`: that Bot's own page, which draws the card of this ask (and is where a Bot that only waits is read);
 * - `view`: the dashboard's page of every session's asks (`HOME_ASKS_PAGE`), for an approval that only the approvals
 *   list carries and for an ask of work that no Bot owns: neither has a card on any Bot's page;
 * - `null`: nowhere. Work that no Bot owns is flagged as waiting and no read carries its request, and a panel cannot
 *   be opened on one session's chat. Such a line is drawn and takes no press.
 */
export type HomeOpens = 'bot' | 'view' | null

/** The dashboard's page that lists the asks of every session with their cards. */
export const HOME_ASKS_PAGE = '/session-dashboards'

/** One thing that waits, as a line: who asks, three words of what, and where a press on it goes. */
export interface HomeLine { id: string; bot: string; who: string; words: string; opens: HomeOpens }

/** Where a press on the line of this ask goes (`HomeOpens`). */
export function lineOpens(need: Pick<XrNeed, 'bot' | 'kind' | 'card'>): HomeOpens {
  if (need.bot && (need.card || need.kind === 'attention')) return 'bot'
  return need.kind === 'attention' ? null : 'view'
}

/**
 * What a press on a line does (D5, the grab). An approval that a board shows is ruled on that board and nowhere
 * else: while its Bot holds the board up, the press takes it, and that same board comes out of Home into his hand
 * (`take`); while it is in his hand or on the floor, the press does nothing more (`none`), so one ask is never on a
 * board at its Bot and in his hand at once, and never on a board and on a page that this press opened. An ask with
 * no board (a question, a Bot's second approval while its first is on the board, work that no Bot owns) opens as
 * the line says (`HomeOpens`).
 */
export type LineDoes = 'take' | 'wait' | 'none' | 'bot' | 'view'

/** A press on a line waits this long for the board that is about to be made (ms); then it does what the line opens. */
export const LINE_WAIT_MS = 2000

/**
 * `awaited`: no board shows this ask yet, and it is the one that its Bot will hold up (the Bot's oldest approval, and
 * the Bot has a body for a board to stand at). The board is made a moment after the read that brought the line; a
 * press in between waits for it (`wait`), and does not open the Bot's page beside the board that then comes.
 * `behindTaken`: the ask waits behind its Bot's board that is in his hand, on the floor or stamped (D24). It waits for
 * that board, and the press does nothing: it does not open the Bot's page beside the board he holds.
 */
export function lineDoes(line: Pick<HomeLine, 'opens'>, board: 'bot' | 'held' | 'floor' | undefined, awaited = false, answered = false, grabs = false, behindTaken = false): LineDoes {
  if (board === 'bot') return 'take'
  if (board) return 'none'
  // An approval that waits behind its Bot's board at the Bot: the line grabs its own board (D24, `grabAsk`).
  if (grabs && !answered) return 'take'
  // A board answered this ask, and the list still names it until it is read again: nothing is left to do with it.
  if (answered || behindTaken) return 'none'
  if (awaited) return 'wait'
  return line.opens ?? 'none'
}

/**
 * Whether an ask waits behind its Bot's board that he has taken (D24): another board of the same Bot is in his hand,
 * on the floor, or stamped. Its line does nothing then (`lineDoes`, `behindTaken`).
 */
export function behindTakenBoard(askId: string, bot: string, boards: readonly { needId: string; bot: string; where: 'bot' | 'held' | 'floor'; stamped: boolean }[]): boolean {
  return boards.some(b => b.bot === bot && b.needId !== askId && (b.where !== 'bot' || b.stamped))
}

/**
 * Whether a board is about to be made for this ask, by the rule the boards' hook makes one by (`board.ts`,
 * `boardsNow`): its Bot's body is drawn, no board of that Bot is in the room, and the ask is the Bot's oldest
 * approval among those that no board answered (`answered`: the hook's own book, `useBoards`, `answered()`, so an
 * id that was answered, left the list and is asked again is a new ask). `oldest` picks that ask (`boardAskOf`).
 */
export function boardComes(askId: string, bot: Pick<XrBot, 'slug' | 'needs'> | undefined, drawn: boolean, botHasBoard: boolean, answered: ReadonlySet<string>, oldest: (bot: Pick<XrBot, 'needs'>) => { id: string } | null): boolean {
  if (!bot || !drawn || botHasBoard) return false
  return oldest({ needs: bot.needs.filter(n => !answered.has(n.id)) })?.id === askId
}

/**
 * The pieces of work that count as done today now, by an id that stays the same while the piece does: the session of
 * a thing, and `own:<slug>` for a Bot's own thread.
 */
export function doneNow(bots: readonly Pick<XrBot, 'slug' | 'state' | 'things' | 'lastTs'>[], nowMs: number): string[] {
  const from = dayStart(nowMs)
  const ids: string[] = []
  for (const b of bots) {
    for (const t of b.things) if (t.state === 'idle' && t.ts >= from) ids.push(t.slot)
    const ownAtRest = !b.things.some(t => t.direct)
    if (ownAtRest && b.state !== 'failed' && b.lastTs >= from && b.things.every(t => t.ts < b.lastTs)) ids.push(`own:${b.slug}`)
  }
  return ids
}

/** A piece of work filed today, as a line of "done today" (D35): who, the first line of its answer, and when it ended. */
export interface DoneLine { id: string; bot: string; slot: string; who: string; words: string; ts: number }

/**
 * The lines of "done today", newest first, `most` at most: each session of a Bot at rest that ended today and that he
 * has read, in XR (thrown) or on the dashboard. One he has not read is not here: it waits on its sheet at its Bot,
 * and Home counts it as to read (`says.toRead`).
 */
export function doneLines(bots: readonly Pick<XrBot, 'slug' | 'label' | 'rest'>[], nowMs: number, most: number = HOME.doneLines): DoneLine[] {
  const from = dayStart(nowMs)
  return bots
    // The answer's first line, whole: it is what tells two lines apart. `Home.tsx` cuts it at the width it has.
    .flatMap(b => b.rest.filter(r => !r.unread && r.ts >= from).map(r => ({ id: `d:${r.slot}`, bot: b.slug, slot: r.slot, who: b.label, words: r.line || r.topic, ts: r.ts })))
    .sort((a, b) => b.ts - a.ts || (a.id < b.id ? -1 : 1))
    .slice(0, most)
}

/** What was counted as done on one day: the day's start, and the ids. */
export interface DoneBook { day: number; ids: ReadonlySet<string> }

/**
 * The book after a read: what was counted today stays counted until the day turns, so the number does not go down
 * when a Bot that had finished starts again, and a piece that rests again is not counted twice. The same book comes
 * back when the read adds nothing.
 */
export function keepDone(book: DoneBook | null, now: readonly string[], nowMs: number): DoneBook {
  const day = dayStart(nowMs)
  if (!book || book.day !== day) return { day, ids: new Set(now) }
  if (now.every(id => book.ids.has(id))) return book
  return { day, ids: new Set([...book.ids, ...now]) }
}

/**
 * The highest the compact strip's top stands, as the eye sees it with the head level (degrees), so that it is whole in
 * the lens (D70 1, M20) with the head turned up to 10 degrees either way (D73). The strip faces straight ahead, so with
 * the head turned its far end comes nearer the lens's plane and stands higher on it: the top CORNERS, 1.24 m apart at
 * about 1.7 m, must stay under the lens's half height (20.35 at the Aura's 40.7), which leaves about 18.9 at the
 * middle (`home.test.ts` works it out). Past a 10 degree turn its end leaves the lens sideways anyway (D74).
 */
export const STRIP_TOP_MOST_DEG = 18.9

export const HOME = {
  /** As far away as a page opens: the eyes keep their focus between Home and a page. */
  distanceM: PAGES.distanceM,
  /** The large form: 39 degrees wide, a digit 5 degrees tall, so each number is read in one look from 1 to 2 m. */
  large: { widthM: 1.24, padM: 0.04, numberM: 0.2, labelM: 0.07, gapM: 0.02, lineM: 0.066, footM: 0.1, switchM: [0.4, 0.08] as const },
  /**
   * The compact form: one strip, its upper edge on the line of sight to the first page's upper edge (so the page's
   * frame and close mark over it stay clear), and `nearerM` nearer than the page on that line, so it is in front.
   */
  compact: { widthM: 1.24, heightM: 0.085, switchM: [0.3, 0.07] as const, nearerM: 0.05, aboveGapM: 0.012 },
  /** The most lines of what waits; the rest is one line that counts them. */
  mostLines: 3,
  /** The most lines of "done today" (D35): the newest; the number counts them all. */
  doneLines: 5,
  /** Canvas pixels for a metre of the surface. */
  pxPerM: 1600,
} as const

/** The start of the local day that `nowMs` is in, in seconds. */
function dayStart(nowMs: number): number {
  const d = new Date(nowMs)
  d.setHours(0, 0, 0, 0)
  return d.getTime() / 1000
}

/**
 * The four numbers, from the Bots as the row draws them and every ask that waits:
 * - `working`: the Bots whose state is working;
 * - `waiting`: the asks that wait for the person (approvals, questions, and a thing flagged as waiting);
 * - `failed`: the Bots whose state is failed as the rims draw it (`rims.ts`): the Bots whose rim is red. A Bot
 *   whose cut he has seen has the dim mark and is not counted, so the number agrees with what he sees in the row;
 * - `done`: the pieces of work that came to rest today (the local day): a thing of a Bot that is at rest and was
 *   last active today, and a Bot's own thread when it is at rest and was the last of that Bot to be active today
 *   (`doneNow`). A piece that was counted today stays counted until the day turns (`keepDone`).
 * shortcut: the model has no time for a Bot's own thread at rest, so that thread counts only when it is newer than
 * all of the Bot's other work; count it by its own time when the model carries one. And "last active" is not
 * "finished": a session that was only opened or read today counts too; count a finish when the model says one.
 */
export function homeStats(bots: readonly Pick<XrBot, 'slug' | 'state' | 'things' | 'lastTs'>[], needs: readonly unknown[], nowMs: number, counted?: ReadonlySet<string>): HomeStats {
  let working = 0
  let failed = 0
  for (const b of bots) {
    if (b.state === 'working') working += 1
    if (b.state === 'failed') failed += 1
  }
  // `counted`: what a book kept of the day (`keepDone`); with none, what counts now.
  return { working, waiting: needs.length, failed, done: counted ? counted.size : doneNow(bots, nowMs).length }
}

/** The first three words of a line; of a line with no spaces (Chinese, Japanese), its first twelve characters. */
export function threeWords(text: string): string {
  const words = text.trim().split(/\s+/).filter(Boolean)
  let chars = [...words.slice(0, 3).join(' ')]
  const most = words.length === 1 ? 12 : 28
  const cut = words.length > 3 || chars.length > most
  if (chars.length > most) chars = chars.slice(0, most)
  const out = chars.join('').replace(/[\s.,;:!?。，；：！？]+$/u, '')
  return cut ? `${out}…` : out
}

/** The lines of what waits, oldest first, and how many more wait than are shown. */
export function homeLines(needs: readonly Pick<XrNeed, 'id' | 'bot' | 'who' | 'action' | 'topic' | 'kind' | 'card'>[], most: number = HOME.mostLines): { lines: HomeLine[]; more: number } {
  // Work that no Bot owns may name nobody: its line then starts with what the work is called.
  const lines = needs.slice(0, most).map(n => ({ id: n.id, bot: n.bot, who: n.who || n.topic, words: threeWords(n.action || n.topic), opens: lineOpens(n) }))
  return { lines, more: Math.max(0, needs.length - lines.length) }
}

/** Large while no page shows; compact while one does, since that page has the place straight ahead. */
export const homeForm = (pagesShown: number): HomeForm => (pagesShown > 0 ? 'compact' : 'large')

/** A rectangle of the surface, in its own plane: its middle (right and up of the surface's middle) and its size, in metres. */
export interface HomeRect { cx: number; cy: number; w: number; h: number }

export interface HomeLayout {
  form: HomeForm
  /** Where the surface's middle is from the head: so far up, so far away, level ahead. */
  pitchDeg: number
  distanceM: number
  widthM: number
  heightM: number
  /** The switch that hides and brings back the real pages. */
  switch: HomeRect
  /** One rectangle for each line of what waits: what a press on that line takes. */
  lines: HomeRect[]
  /** The row that counts what waits beyond the lines, or `null`. */
  more: HomeRect | null
  /** The middles of the four numbers (large), left to right. */
  cells: { cx: number; numberY: number; labelY: number }[]
  /** The row that heads "done today" and says how many wait to be read, or `null`; and one row for each done line. */
  doneHead: HomeRect | null
  done: HomeRect[]
}

/** What Home shows of the done work besides its number: whether its head row stands, and how many lines under it. */
export interface HomeDoneRows { head: boolean; lines: number }
const NO_DONE: HomeDoneRows = { head: false, lines: 0 }

/**
 * How the surface is laid out with `lineCount` lines of what waits, and `more` set when a row counts the rest; under
 * them, in the large form, the rows of "done today" (`done`). `above`: the strip stands over the first page's upper
 * edge instead of in front of its uppermost rows, so the page's own bar is read. `cutM`: what the compact strip gives
 * up of its right end so that no close mark's press area lies on it (D54, `homePlace.stripCut`); the switch stays at
 * the right end.
 */
/**
 * A first page's upper edge as the eye sees it where the page opens (`PAGES.lowerDeg` under level, facing him): its
 * angle up (radians) and how far it is along that line.
 */
export function firstPageTop(): { pageTop: number; lineM: number } {
  const l = (PAGES.lowerDeg * Math.PI) / 180
  const halfM = (PAGES.cssHeight * M_PER_PX) / 2
  const y = PAGES.distanceM * Math.sin(l) + halfM * Math.cos(l)
  const z = PAGES.distanceM * Math.cos(l) - halfM * Math.sin(l)
  return { pageTop: Math.atan2(y, z), lineM: Math.hypot(y, z) }
}

export function homeLayout(form: HomeForm, lineCount: number, more: boolean, above = false, done: HomeDoneRows = NO_DONE, cutM = 0): HomeLayout {
  if (form === 'compact') {
    const c = HOME.compact
    // The line of sight to the first page's upper edge where the page opens (its pitch, D74), and the strip's own half
    // height as an angle on it.
    const { pageTop, lineM } = firstPageTop()
    const distanceM = lineM - c.nearerM
    // Above the page it stands over the band that takes the page's top edge (`PAGES.edgeDeg`, D65), with air between,
    // when that leaves its top in the lens with the head at rest (D70 1, `STRIP_TOP_MOST_DEG`). Else its top stands at
    // that most, in front of the top band, and takes the dot there: the page is carried by its sides and bottom.
    const bandM = PAGES.distanceM * Math.tan((PAGES.edgeDeg * Math.PI) / 180)
    const half = Math.atan2(c.heightM / 2, distanceM)
    const most = (STRIP_TOP_MOST_DEG * Math.PI) / 180 - half
    const pitch = above ? Math.min(pageTop + Math.atan2(c.heightM / 2 + bandM + c.aboveGapM, distanceM), most) : pageTop - half
    const widthM = c.widthM - cutM
    return {
      form, pitchDeg: (pitch * 180) / Math.PI, distanceM, widthM, heightM: c.heightM,
      switch: { cx: widthM / 2 - c.switchM[0] / 2 - 0.01, cy: 0, w: c.switchM[0], h: c.switchM[1] },
      lines: [], more: null, cells: [], doneHead: null, done: [],
    }
  }
  const l = HOME.large
  const asks = lineCount + (more ? 1 : 0)
  const doneRows = done.head || done.lines ? 1 + done.lines : 0
  const rows = asks + doneRows
  const heightM = l.padM + l.numberM + l.labelM + l.gapM + rows * l.lineM + (rows ? l.gapM : 0) + l.footM + l.padM / 2
  const top = heightM / 2
  const firstLine = top - l.padM - l.numberM - l.labelM - l.gapM
  const row = (i: number): HomeRect => ({ cx: 0, cy: firstLine - (i + 0.5) * l.lineM, w: l.widthM - 2 * l.padM, h: l.lineM })
  return {
    form, pitchDeg: 0, distanceM: HOME.distanceM, widthM: l.widthM, heightM,
    switch: { cx: l.widthM / 2 - l.padM - l.switchM[0] / 2, cy: -top + l.padM / 2 + l.footM / 2, w: l.switchM[0], h: l.switchM[1] },
    lines: Array.from({ length: lineCount }, (_, i) => row(i)),
    more: more ? row(lineCount) : null,
    doneHead: doneRows ? row(asks) : null,
    done: doneRows ? Array.from({ length: done.lines }, (_, i) => row(asks + 1 + i)) : [],
    cells: [0, 1, 2, 3].map(i => ({ cx: -l.widthM / 2 + ((i + 0.5) * l.widthM) / 4, numberY: top - l.padM - l.numberM / 2, labelY: top - l.padM - l.numberM - l.labelM / 2 })),
  }
}
