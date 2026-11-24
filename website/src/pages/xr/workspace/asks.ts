/**
 * A Bot's ask comes up by itself: which ask, when, and what a press on its panel does. Plain rules with no clock of
 * their own and nothing drawn; `Workspace.tsx` feeds them and acts on what they say.
 *
 * An ask that comes up is an entry of a Bot's `needs` whose card the Bot's own page draws (`model.ts`, `card`): an
 * approval that stands on a session's row, in the Bot's own thread or in a session of work that the Bot handed
 * out, and a question card. It comes up on the ask's panel of that Bot: the Bot's page as a narrow page, straight
 * ahead of the eye and nearer than everything that stands there. Nothing turns.
 *
 * What is new and what is gone is told only from a whole read (`wholeRead`): while a list is loading, failed or a
 * part of itself, and while the live connection is down, nothing counts as new and nothing counts as gone.
 */
import { wholeRead, type XrCrew } from '../model'
import { passesHandBack } from './board'
import type { PageSource } from './source'

export const ASKS = {
  /** An ask comes up when the person has not been busy for so long. */
  calmMs: 2000,
  /** Keys that went to a page count as busy for so long. */
  keysMs: 5000,
  /** The wheel that turned on a page counts as busy for so long: a panel that came up under the pointer would take the next notch. */
  wheelMs: 2000,
  /** After every ask of its Bot is gone, an ask's panel stays so long, and then closes. */
  doneMs: 3000,
  /** An ask's panel takes no press until so long after its first picture, or its first since it came up again. */
  firstPictureMs: 1000,
  /** On a Bot's page, while that Bot asks: no press for so long after a press there, sent or held back. */
  afterPressMs: 500,
  /** On a Bot's page: no press for so long after that Bot's asks changed. */
  afterChangeMs: 1000,
  /** No press on any panel for so long after an ask's panel closed by itself: its place is not what was aimed at. */
  afterCloseMs: 500,
  /** An ask's panel takes no press for so long after the controls that answer an ask changed in number or in place on its page. */
  afterAnswersMs: 1000,
  /** While an ask's panel stands, its page is asked so often where those controls are. */
  answersEveryMs: 300,
  /** The ask's panel is the Bot's page so wide, in CSS pixels: the dashboard's own phone layout, the chat alone. */
  cssWidth: 390,
} as const

/* ---------------- what is seen, new and gone ---------------- */

/** What the XR page knows of the asks since it was loaded. */
export interface AskBook {
  /** A whole read was seen: from then on an ask that was not there is new. */
  started: boolean
  /**
   * Every ask with a card that stands, with the order it was seen in. One that is gone is forgotten: an ask of the
   * same name later is a new ask (a session's backend counts its asks from one again when it starts again).
   */
  seen: Map<string, number>
  /** How many asks were seen in all: the next one's place in the order. */
  order: number
  /** The asks that wait to come up, each with its Bot's slug. */
  waiting: Map<string, string>
  /** Each Bot's asks with a card as of the last whole read, by id, in the model's order. */
  cards: Map<string, string[]>
  /** How many asks each Bot has in all, with a card or with none, as of the last whole read. */
  counts: Map<string, number>
}

export const newBook = (): AskBook => ({ started: false, seen: new Map(), order: 0, waiting: new Map(), cards: new Map(), counts: new Map() })

/**
 * Take a read of the crew into the book. Returns the Bots whose asks with a card are others than before (one
 * more, one fewer, another): a card on their page has changed place or gone. From a read that is not whole
 * nothing is taken, and nothing has changed.
 */
export function readAsks(book: AskBook, crew: XrCrew): string[] {
  if (!wholeRead(crew.feeds)) return []
  const changed: string[] = []
  const now = new Set<string>()
  const cards = new Map<string, string[]>()
  const counts = new Map<string, number>()
  for (const bot of crew.bots) {
    const ids = bot.needs.filter(n => n.card).map(n => n.id)
    cards.set(bot.slug, ids)
    counts.set(bot.slug, bot.needs.length)
    for (const id of ids) {
      now.add(id)
      if (book.seen.has(id)) continue
      book.seen.set(id, book.order)
      book.order += 1
      // What was there at the first whole read does not come up by itself: it shows by the Bot's plate.
      if (book.started) book.waiting.set(id, bot.slug)
    }
    const before = book.cards.get(bot.slug) ?? []
    if (book.started && (before.length !== ids.length || before.some((id, i) => id !== ids[i]))) changed.push(bot.slug)
  }
  // A Bot that left the crew took its asks with it.
  for (const slug of book.cards.keys()) if (!cards.has(slug) && (book.cards.get(slug)?.length ?? 0) > 0) changed.push(slug)
  // An ask that is gone before its moment never comes up. It is forgotten: the same name later is a new ask.
  for (const id of [...book.waiting.keys()]) if (!now.has(id)) book.waiting.delete(id)
  for (const id of [...book.seen.keys()]) if (!now.has(id)) book.seen.delete(id)
  book.cards = cards
  book.counts = counts
  book.started = true
  return changed
}

/**
 * The asks that may come up on a panel now (D24, one board at a time): none that a board holds, and none of a Bot
 * whose board stands (at the Bot, in his hand or on the floor), its workers' included. Those wait behind the board,
 * and come up when it ends: as its next board when it is an approval, on its panel when it is a question.
 */
export function freeAsks(waiting: ReadonlyMap<string, string>, hold: AskHold): Map<string, string> {
  return new Map([...waiting].filter(([id, bot]) => !heldBack(id, bot, hold)))
}

/**
 * What holds an ask back from its panel: the asks on a board (`onBoards`), the Bots whose board stands (`boardBots`),
 * and of those the Bots whose board he handed back himself (`handedBack`, D27), whose questions and `attention` asks (`kindOf`, `passesHandBack`) do not wait.
 */
export interface AskHold {
  onBoards: ReadonlySet<string>
  boardBots: ReadonlySet<string>
  handedBack?: ReadonlySet<string>
  kindOf?: (id: string) => string | undefined
}

/** Whether an ask waits behind a board now (D24, D27). */
export function heldBack(id: string, bot: string, hold: AskHold): boolean {
  if (hold.onBoards.has(id)) return true
  if (!hold.boardBots.has(bot)) return false
  return !(hold.handedBack?.has(bot) && passesHandBack(hold.kindOf?.(id)))
}

/** How many asks with a card a Bot has, as of the last whole read. */
export const cardsOf = (book: AskBook, bot: string): number => book.cards.get(bot)?.length ?? 0

/** The asks of a Bot that wait to come up are done with: its panel shows them, or the person put them off. */
export function settle(book: AskBook, bot: string): void {
  for (const [id, slug] of [...book.waiting]) if (slug === bot) book.waiting.delete(id)
}

/** The Bots that have an ask, the one whose ask was seen first in front: the order their plates are given room in. */
export function askingBots(book: AskBook): string[] {
  const first = (slug: string) => Math.min(...(book.cards.get(slug) ?? []).map(id => book.seen.get(id) ?? Infinity), Infinity)
  return [...book.counts].filter(([, n]) => n > 0).map(([slug]) => slug).sort((a, b) => first(a) - first(b))
}

/* ---------------- which ask comes up now ---------------- */

/** An ask's panel that stands, as the rules read it. */
export interface AskPanelNow {
  bot: string
  /** Its middle is in the picture; in a session, it is in view. */
  inView: boolean
  /** Some part of it is in the picture; in a session, in view. */
  partInView: boolean
}

export interface AskMoment {
  now: number
  /** Which way the gateway's panels hold: nothing comes up until the XR page has heard that they are open. */
  way: 'open' | 'guarded' | null
  /** Since when the person has not been busy. `null`: busy now. */
  calmSince: number | null
  /** The ask's panels that stand and are not the person's pages. */
  panels: readonly AskPanelNow[]
  /** How many pages are open, and how many the gateway holds. */
  open: number
  most: number
  /** An open was refused or a page failed: nothing more comes up until a page has opened by a tap. */
  stopped: boolean
}

export type AskStep =
  | { do: 'nothing' }
  /** Open the ask's panel of this Bot, straight ahead. */
  | { do: 'up'; bot: string }
  /** The ask's panel of this Bot stands out of view: it comes up again, straight ahead. */
  | { do: 'again'; bot: string }
  /** No page can open: say once, for this ask, that the room is full. */
  | { do: 'full'; bot: string; ask: string }

/**
 * What to do now for the asks that wait. The ask that was seen first goes first. An ask whose Bot's panel stands
 * with a part of it in the picture is shown there, and is done with here (the book is changed): nothing moves,
 * since a panel that left its place while its buttons showed would send a press that was on its way to them to
 * the page behind it. Only a panel of which no part is in the picture comes up again. The next ask waits while an
 * ask's panel stands in view, and while the person is busy.
 */
export function nextStep(book: AskBook, at: AskMoment): AskStep {
  if (!book.started || at.way !== 'open' || at.stopped) return { do: 'nothing' }
  const oldest = () => [...book.waiting].sort((a, b) => (book.seen.get(a[0]) ?? 0) - (book.seen.get(b[0]) ?? 0))[0]
  for (let next = oldest(); next; next = oldest()) {
    const [ask, bot] = next
    const own = at.panels.find(p => p.bot === bot)
    if (own?.partInView) { settle(book, bot); continue }
    // One at a time: while an ask's panel stands in view, the next ask waits.
    if (at.panels.some(p => p.inView)) return { do: 'nothing' }
    if (at.calmSince === null || at.now - at.calmSince < ASKS.calmMs) return { do: 'nothing' }
    if (own) return { do: 'again', bot }
    if (at.open >= at.most) return { do: 'full', bot, ask }
    return { do: 'up', bot }
  }
  return { do: 'nothing' }
}

/* ---------------- a press on a panel ---------------- */

/** What the gate knows of a panel when a press ends on its page. */
export interface PressFacts {
  now: number
  /** When the panel's first picture came, since it opened or since it came back into view. `null`: none yet. */
  pictureAt: number | null
  /** The panel is an ask's panel, and not the person's page. */
  ask: boolean
  /** Every ask of that ask's panel's Bot is gone: the panel waits to close. */
  done: boolean
  /** The panel shows a Bot's page. */
  botPage: boolean
  /** That Bot has an ask with a card now. */
  botAsks: boolean
  /** When a press last ended on this panel, sent or held back. `null`: never. */
  pressAt: number | null
  /** When that Bot's asks last changed in the model. `null`: never. */
  changedAt: number | null
  /** When an ask's panel last closed by itself, or the lens last went back after one. `null`: never. */
  closedAt: number | null
  /**
   * When the controls that answer an ask last changed in number or in place on this panel's page (`takeAnswers`).
   * `null`: the page has not said yet where they are. Read for an ask's panel only. Left out by a panel whose
   * source cannot say: nothing is waited for.
   */
  answersAt?: number | null
}

/**
 * What becomes of a press: it is sent to the page, it is held back and does nothing, or it only keeps the panel.
 *
 * A press must do what the picture shows. So no panel takes one before its first picture, or before its first
 * since it came back into view. Nothing takes one right after an ask's panel closed by itself, where another page
 * now shows through. An ask's panel takes none in its first second: a finger that was on its way elsewhere must
 * not answer. On a Bot's page a card that is decided goes and the next one moves up to its place, so a press there
 * waits half a second after the press before it, and a second after the Bot's asks changed. And once nothing is
 * asked any more, a press on the ask's panel keeps it and goes no farther.
 */
/**
 * When the picture that a press would land on came, as the gates read it: null while the page takes no input. A
 * source that says when it takes input (`notYet`) and has no picture time once its wait ended takes input (0): its
 * waits end by themselves, and the gates end with them (D53 1). No panel takes a press before its first picture: a
 * source holds (`notYet`) until a picture of its open landed (D55 1).
 */
export function pictureFor(s: Pick<PageSource, 'notYet' | 'pictureAt'>): number | null {
  return s.notYet ? (s.notYet() ? null : s.pictureAt?.() ?? 0) : s.pictureAt ? s.pictureAt() : 0
}

/**
 * Whether a press on a panel's page would be dropped now, for the dot's light (D53 2): a page being handed, a page in
 * its calm time, the quiet time after a close, or the ask gate's hold. Writes nothing.
 */
export function pressHeldNow(id: string, now: number, w: { handing: ReadonlyMap<string, string>; calmUntil: ReadonlyMap<string, number>; quiet: (now: number) => boolean; asks: ((id: string) => boolean) | null }): boolean {
  if (w.handing.get(id) === 'handing' || (w.calmUntil.get(id) ?? 0) > now) return true
  if (w.quiet(now)) return true
  return w.asks?.(id) ?? false
}

export function pressFate(f: PressFacts): 'send' | 'hold' | 'keep' {
  if (f.pictureAt === null) return 'hold'
  if (f.closedAt !== null && f.now - f.closedAt < ASKS.afterCloseMs) return 'hold'
  if (f.ask && f.done) return 'keep'
  if (f.ask && f.now - f.pictureAt < ASKS.firstPictureMs) return 'hold'
  // A card that came, went or moved under the finger: its buttons stand where something else stood a moment ago.
  if (f.ask && f.answersAt !== undefined && (f.answersAt === null || f.now - f.answersAt < ASKS.afterAnswersMs)) return 'hold'
  if (!f.botPage) return 'send'
  if (f.botAsks && f.pressAt !== null && f.now - f.pressAt < ASKS.afterPressMs) return 'hold'
  if (f.changedAt !== null && f.now - f.changedAt < ASKS.afterChangeMs) return 'hold'
  return 'send'
}

/** What an ask's panel's page last said of the controls that answer an ask: where they are, and since when. */
export type AnswerBook = Map<string, { places: string; at: number }>

/**
 * Take a page's word on where the controls that answer an ask are drawn (`source.ts`, `answerPlaces`). Says whether
 * they changed in number or in place since its word before; the first word is a change. The time of the change
 * is what the gate counts its second from (`pressFate`, `answersAt`). A page that could not be asked (`null`) has
 * said nothing: what it said before stands.
 *
 * The page's whole text is not read: while a Bot writes it changes all the time, and a card of work that the Bot
 * handed out could then not be answered for as long as it writes. The controls are an approval's buttons, which
 * carry the grant mark, and a question card's Dismiss and Submit, which carry the XR page's own mark
 * (`panel_page.py`, `ANSWER_ATTR`). shortcut: a card that takes another's place in one step with its controls at
 * the same places is not seen; upgrade when the page can name its cards.
 */
export function takeAnswers(book: AnswerBook, id: string, places: string | null, now: number): boolean {
  if (places === null || book.get(id)?.places === places) return false
  book.set(id, { places, at: now })
  return true
}

/**
 * The places that a press on an ask's panel carries to the gateway: where the page last said the controls that
 * answer an ask are (`takeAnswers`). The gateway looks at the page once more right before it sends the press, and
 * sends none when they stand elsewhere now (`panel_hub.py`, `_answers_moved`): a card drawn in the round trip
 * after the last word is not pressed. `undefined`: nothing was heard, and the press carries no such word.
 */
export function pressPlaces(heard: string | null): number[][] | undefined {
  if (heard === null) return undefined
  try {
    const places: unknown = JSON.parse(heard)
    const ok = Array.isArray(places) && places.length <= 40 && places.every(p => Array.isArray(p) && p.length === 4 && p.every(v => typeof v === 'number' && Number.isFinite(v)))
    return ok ? places as number[][] : undefined
  } catch { return undefined }
}

/** What a tap on a Bot does while it may ask (`useAskPanels.ts`, `tapBot`). */
export interface TapFacts {
  /** The ask's panels are open in this build. */
  open: boolean
  /** That Bot's cards that wait. */
  cards: number
  /** That Bot's ask's panel that stands now, and whether a part of it is in the picture. */
  standing: string | null
  partInView: boolean
}

/**
 * A tap on a Bot that asks: the id of its ask's panel, for the picture to turn to, where a part of it shows;
 * `true` when the panel came up (`comeUp`); `false` when the tap does what it does for a Bot that asks nothing:
 * its own page opens, the card is on it, and the person answers there. That is also the answer when the panel
 * could not come up: a page in the way, or a full room (whose own line then says why no page opens).
 */
export function tapAnswer(f: TapFacts, comeUp: () => boolean): boolean | string {
  if (!f.open || f.cards === 0) return false
  if (f.standing !== null && f.partInView) return f.standing
  return comeUp()
}

/** What the lens was before an ask set it back to the start's, and what the ask set it to. */
export interface LensBack { from: number; to: number }

/**
 * An ask's panel came up and the lens is `to` now, where it was `before`. Returns what to go back to later: the
 * lens from before the first ask when asks follow one another, and nothing when the ask did not move the lens.
 */
export const lensSetBack = (kept: LensBack | null, before: number, to: number): LensBack | null => kept ?? (lensMoved(before, to) ? { from: before, to } : null)

/**
 * Whether an ask's coming up moved the lens. When it did, every page is drawn smaller at once, and a press that
 * set out for a thing on the person's own page would land beside it: the quiet time starts, as for a turn, and
 * no page, panel or Bot takes a press in the half second after (`ASKS.afterCloseMs`).
 */
export const lensMoved = (before: number, to: number): boolean => before !== to

/**
 * Whether the lens goes back now, and to what: every ask's panel is gone and none is about to come up, and the
 * lens is still where the ask put it. (A person who changed it meanwhile, or kept the panel, has `kept` taken
 * away by the workspace: the home button puts the lens where an ask puts it, so the number alone cannot tell.)
 */
export function lensGoesBack(kept: LensBack | null, at: { near: number; panels: number; coming: boolean }): number | null {
  if (!kept || at.panels > 0 || at.coming) return null
  return at.near === kept.to ? kept.from : null
}

/**
 * What becomes of a notch of the wheel on a panel's page. An ask's panel takes none before its first picture and in
 * the second after it (the first since it came up again, too): a notch that was on its way to the person's page
 * must not scroll the Bot's chat, which could take the card away from where its buttons can be pressed. On every
 * other panel the wheel is the page's, as it was.
 */
export function wheelFate(f: Pick<PressFacts, 'now' | 'pictureAt' | 'ask'>): 'send' | 'hold' {
  if (!f.ask) return 'send'
  return f.pictureAt === null || f.now - f.pictureAt < ASKS.firstPictureMs ? 'hold' : 'send'
}

/**
 * Whether the person is busy, so that no ask comes up: something is held (a finger or a button is down, two
 * fingers move, the quiet time runs, the picture turns by itself), keys went to a page in the last five seconds,
 * or the wheel turned on a page in the last two.
 */
export function isBusy(at: { now: number; held: boolean; lastKeyAt: number; lastWheelAt: number }): boolean {
  return at.held || at.now - at.lastKeyAt < ASKS.keysMs || at.now - at.lastWheelAt < ASKS.wheelMs
}

/**
 * Whether this makes an ask's panel the person's page: it then stays, is kept with the room, and is no ask's panel
 * any more. A key that the person typed into it does, at any time: he is talking with the Bot. A press or a wheel
 * on it does once nothing is asked any more (`done`), in the seconds before it would close. A wheel or a drag
 * while the ask waits does not: a person scrolls to read the card.
 */
export const keepsPanel = (what: 'press' | 'key' | 'wheel', done: boolean): boolean => what === 'key' || done

/* ---------------- where it comes up, and how big ---------------- */

/** One picture px of the flat preview is this many metres at 1 m, through a lens of `fovDeg` up and down on a canvas `heightPx` tall. */
const mPerScreenPxAt1M = (heightPx: number, fovDeg: number): number => (2 * Math.tan((fovDeg * Math.PI) / 360)) / Math.max(1, heightPx)

/** The air that the flat preview's ask's panel keeps from the picture's edges, in screen px. */
export const ASK_AIR_PX = 6

/**
 * The ask's panel in the flat preview: how big its page is in CSS pixels, and how far from the eye it stands.
 *
 * A page px has one size in the room (`mPerPx`), so nearness gives the buttons their size. The panel stands where
 * one page px is one screen px, which is a button of the page's own size (the smallest is 78 by 29 px), or farther
 * where the picture is too small for the whole panel at that size: its width with a little air at each side, its
 * height with the close mark over it. The page is as tall as the picture has room for, within what the Bot's page
 * needs for a card and what reads as a small page. `lowPx` of the picture's lower end are not the panel's: the
 * phone's own field for typing stands there while the person types in a page.
 */
export function askPanelFlat(view: { width: number; height: number; fovDeg: number }, mPerPx: number, markPx = 44, lowPx = 0): { css: [number, number]; distanceM: number; scale: number } {
  const air = ASK_AIR_PX
  const width = ASKS.cssWidth
  const room = view.height - lowPx - markPx - 2 * air
  const height = Math.round(Math.max(480, Math.min(700, room)))
  const scale = Math.min(1, (view.width - 2 * air) / width, room / height)
  return { css: [width, height], distanceM: mPerPx / (scale * mPerScreenPxAt1M(view.height, view.fovDeg)), scale }
}

/**
 * The ask's panel in a session: its page's size, and how far from the head it stands. A hand's ray holds 1.7
 * degrees: at 1.4 m a button 29 px high is 1.8 degrees high and one 78 px wide is 4.8 wide.
 */
export const ASK_SESSION = { css: [ASKS.cssWidth, 560] as [number, number], distanceM: 1.4 } as const

/* ---------------- nearer than everything that stands there ---------------- */

type V3 = readonly [number, number, number]
const sub = (a: V3, b: V3): V3 => [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
const dot = (a: V3, b: V3): number => a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
const cross = (a: V3, b: V3): V3 => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
const unit = (a: V3): V3 => { const n = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / n, a[1] / n, a[2] / n] }

/** How much nearer than the nearest page or other ask's panel behind it an ask's panel comes up. */
export const ASK_CLEAR_M = 0.05
/** How much nearer than a Bot's front: nothing of a Bot is read or pressed through a panel, so it only has to be clear of it. */
export const ASK_CLEAR_BOT_M = 0.01
/**
 * The bar of the flat preview is a button, not the panel's rim. A card's row of buttons begins 30 page px in from
 * the page's side: nearer than the measure asks, each side of the page may leave the picture by less than that.
 */
export const ASK_SIDE_CUT_PX = 29

/**
 * The nearest an ask's panel may stand in the flat preview: no nearer than a hand can push a page (`nearM`), and no
 * nearer than where the card's buttons are still whole in the picture. The page's lower end, where the buttons
 * are, is held where it is (`AskPlaceAsk.low`), so only the picture's width is a bound.
 */
export function askLeastFlat(view: { width: number }, size: { css: [number, number]; distanceM: number; scale: number }, nearM: number): number {
  const most = view.width / (size.css[0] - 2 * ASK_SIDE_CUT_PX)
  return Math.max(nearM, (size.distanceM * size.scale) / most)
}

export interface AskPlaceAsk {
  /**
   * Where the eye is, and the way the panel's face looks away from it, of length one. In a session that is the
   * line straight ahead of the head. On a screen it is the lens's own axis: a panel that is square to it is drawn
   * upright and at one size all over, where one that faced the eye from under the axis would be drawn wider at
   * its lower edge.
   */
  eye: V3
  ahead: V3
  /** The panel's own right and up, of length one. Left out: level, with no roll. */
  right?: V3
  up?: V3
  /**
   * Where the panel's middle is off that line: so far to the right and so far up for each metre ahead. On a
   * screen whose frame is shifted the picture's middle is not on the lens's axis. Left out: on the line.
   */
  slope?: readonly [number, number]
  /**
   * Given: the panel's LOWER END, not its middle, is so far up for each metre ahead (and `slope`'s second number
   * is not read). A panel that comes nearer then grows upward: the card's buttons, at the page's lower end, stay
   * where they are in the picture, and the upper rim and the close mark are what leave it first.
   */
  low?: number
  /**
   * With `low`: how far up for each metre ahead the panel's lower end may go down to, and its upper end up to (the
   * picture's lower end and the place under its upper end that leaves the close mark its room). A panel that comes
   * nearer then grows about its middle as far as these allow; only then does its lower end stay, at the lowest.
   */
  ends?: readonly [number, number]
  /** The panel's size in the room, and how far ahead it would stand with nothing in the way. */
  widthM: number
  heightM: number
  distanceM: number
  /** The nearest it may stand: never nearer than a hand can push a page, and on a screen no nearer than where its buttons are still whole in the picture. */
  leastM: number
  /** Points of every page and other ask's panel that it would cover: it stands `ASK_CLEAR_M` nearer than the nearest. */
  things: readonly V3[]
  /** Points of the Bots' fronts that it would cover: it stands `ASK_CLEAR_BOT_M` nearer than the nearest. */
  bots?: readonly V3[]
  /** Points of the drawn room's desk, when the room is on: the panel is raised clear of them. */
  desk?: readonly V3[]
}

/**
 * Where an ask's panel comes up: its middle, or `null` when something stands in the way.
 *
 * "There" is its whole face: of every page that the eye would see through that face, it stands `ASK_CLEAR_M`
 * nearer than the nearest, and `ASK_CLEAR_BOT_M` nearer than the nearest Bot's front. A nearer panel covers more of
 * the view, so the look is taken again until it holds. When that is nearer than `leastM`, it does not come up.
 *
 * The desk of the drawn room is not come nearer to: its near rim would hide a strip of a panel that stands at the
 * wanted distance with the head turned down to the crew, and nearer than that rim a panel is too near to read.
 * The panel is raised instead, by as little as takes every point of the desk that is nearer than it out of its face.
 */
export function askPlace(ask: AskPlaceAsk): { at: V3; distanceM: number; raisedM: number } | null {
  const ahead = unit(ask.ahead)
  // Level, with no roll, when no other way is given: its right is level, and its up is square to both.
  const right = ask.right ? unit(ask.right) : unit(Math.abs(ahead[1]) > 0.999 ? [1, 0, 0] : cross(ahead, [0, 1, 0]))
  const up = ask.up ? unit(ask.up) : cross(right, ahead)
  const [sx, sy] = ask.slope ?? [0, 0]
  /** How far up the panel's middle is, standing `d` away. */
  const mid = (d: number): number => {
    if (ask.low === undefined) return sy * d
    if (!ask.ends) return ask.low * d + ask.heightM / 2
    // Where its middle is at its own distance, on the same line; held under the upper bound, and the lower one wins.
    const wish = (ask.low + ask.heightM / (2 * ask.distanceM)) * d
    return Math.max(ask.ends[0] * d + ask.heightM / 2, Math.min(wish, ask.ends[1] * d - ask.heightM / 2))
  }
  /** How deep the nearest of `points` is that the eye would see through the panel's face, standing `d` away and raised by `lift`. */
  const nearest = (points: readonly V3[], d: number, lift: number): number => {
    let least = Infinity
    for (const p of points) {
      const u = sub(p, ask.eye)
      const deep = dot(u, ahead)
      if (deep <= 0.01) continue
      const k = d / deep
      if (Math.abs(dot(u, right) * k - sx * d) <= ask.widthM / 2 && Math.abs(dot(u, up) * k - mid(d) - lift) <= ask.heightM / 2) least = Math.min(least, deep)
    }
    return least
  }
  let d = ask.distanceM
  let lift = 0
  for (let look = 0; look < 8; look += 1) {
    // In steps of a centimetre, to at most the panel's own height: past that it would not be straight ahead at all.
    lift = 0
    while (ask.desk && lift <= ask.heightM && nearest(ask.desk, d, lift) < d + ASK_CLEAR_M) lift += 0.01
    if (lift > ask.heightM) return null
    const most = Math.min(nearest(ask.things, d, lift) - ASK_CLEAR_M, nearest(ask.bots ?? [], d, lift) - ASK_CLEAR_BOT_M)
    if (most >= d - 1e-9) break
    d = most
  }
  if (!(d >= ask.leastM)) return null
  return { at: [0, 1, 2].map(i => ask.eye[i] + ahead[i] * d + right[i] * sx * d + up[i] * (mid(d) + lift)) as unknown as V3, distanceM: d, raisedM: lift }
}

/** Points all over a flat rectangle in the room: its middle, its two half sides as vectors. A page, a panel, a Bot's body as the eye sees it. */
export function facePoints(middle: V3, halfRight: V3, halfUp: V3, across = 16, down = 12): V3[] {
  const points: V3[] = []
  for (let i = 0; i <= across; i += 1) for (let j = 0; j <= down; j += 1) {
    const a = (2 * i) / across - 1, b = (2 * j) / down - 1
    points.push([middle[0] + halfRight[0] * a + halfUp[0] * b, middle[1] + halfRight[1] * a + halfUp[1] * b, middle[2] + halfRight[2] * a + halfUp[2] * b])
  }
  return points
}
