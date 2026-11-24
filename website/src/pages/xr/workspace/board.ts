/**
 * The board: a Bot that waits on an approval holds up a board with what it asks.
 * He takes it and reads it. A stamp on it is the yes. A throw with no stamp is the no. Handing it back is no answer.
 * After the stamp the board stays in his hand; thrown, it falls apart into particles. Plain rules and pure functions;
 * `Board.tsx` draws the boards and `Workspace.tsx` holds them.
 *
 * - One board per ask (`need.id`), approvals only, and a Bot holds up its oldest one.
 * - A board is bound to the ask's `version` when it is taken. A stamp on a board whose ask changed is refused: the
 *   board shows the new text and takes no stamp for a second. An ask that was answered elsewhere, or withdrawn, ends
 *   the board with one line.
 * - No stamp before the board is drawn in his hand, nor in its first second there.
 * - The text is one sentence, "<who>: <action>", and under it the exact command (or the tool's name), whole in his
 *   hand, so every stamp on the paper is the yes.
 * - A throw is a move far and still fast at the release; less is a hand-back, a release on its own Bot too; a click
 *   with no move does nothing.
 * - A throw with no stamp lies on the floor for `floorMs`, and can be picked up again; the no goes when that time ends,
 *   unless its ask changed meanwhile: then the board goes back to its Bot.
 * - A board that stood out of his view for `awayMs` goes back to its Bot: no answer.
 * - The stamp sends one decision with one `responseId`, which a retry of the stamp reuses, so a retry is never a
 *   second answer. The no mints its own id: the gateway replays by the id alone.
 * - A Bot's words are a board too (`kind: 'words'`, `says.ts`): sheets to read, with no stamp. An approval of its Bot
 *   comes first; a throw sends nothing and falls apart, a hand-back keeps it at the Bot.
 */
import type { XrBot, XrNeed } from '../model'
import { answerOf, answerable, answersAtPrint, anyPick, noteFor, pick, seenAt, stillOpen, type Picks } from './question'
import type { Print } from './boardPicture'
import type { Says, Sheet } from './says'

export const BOARD = {
  /** No stamp in this first time after the board came into his hand, or after its text changed. */
  calmMs: 1000,
  /** A board thrown with no stamp lies on the floor this long; then the no goes. */
  floorMs: 4000,
  /** A words board he handed back stands on its Bot's clipboard this long before it is lowered. */
  lowerAfterMs: 400,
  /** Out of his view this long, a held board goes back to its Bot. */
  awayMs: 60_000,
  /**
   * In the preview a release is a throw when the board travelled this far in px, moved this fast over the last
   * `speedMs` of its movement, and its last move came at most `lastMoveMs` before the release: a drag that slows or
   * stops to place the board is no throw.
   */
  throwPx: 140, throwPxPerMs: 0.6, speedMs: 70, lastMoveMs: 60,
  /** In a session: the hand travelled this far while the pinch was held, and moved this fast over its last 100 ms. */
  throwM: 0.09, throwMps: 0.8, handSpeedMs: 100,
  /** A release that moved less than this (px in the preview, the hand's metres in a session) does nothing. */
  stayPx: 6, stayM: 0.015,
} as const

export type BoardWhere = 'bot' | 'held' | 'floor'

export interface Board {
  /** The ask it shows, and the version it was taken with. */
  needId: string
  bot: string
  version: string
  where: BoardWhere
  /** When it came into his hand, or its text last changed: the stamp waits `calmMs` from the later. */
  calmFrom: number
  /** The stamp went: the yes is on its way or sent. */
  stamped: boolean
  /** When it was thrown to the floor. */
  floorAt: number
  /**
   * The one id of its yes, minted at the stamp and reused on a retry of the stamp. The no mints its own (`noId`): the
   * gateway replays a response by its id alone, so a no with the yes's id would be answered with the yes.
   */
  responseId: string
  noId: string
  /** Since when it stands out of his view, or `null`. */
  awaySince: number | null
  /**
   * He handed it back himself (D27): his "not now" for this one ask, not for what waits behind it. While it stands so
   * at its Bot, the Bot's questions no longer wait behind it; its approvals still do (D24).
   */
  back: boolean
  /**
   * Put by (D83 1): a third take put it back to its Bot. Not his "not now": nothing of its Bot comes past it, and it
   * stands there with his note, prints and marks while its ask lives (a sheet: while it carries his note).
   */
  put?: boolean
  /** Its ask was answered elsewhere and it only keeps his unsent note (F2). */
  elsewhere?: boolean
  /**
   * What it shows: an ask to sign, its Bot's words (`says.ts`), read sheet by sheet, or the blank sheet that its Bot
   * holds up while he holds a pinch on it and speaks (`speak`, P2): his words are its note (`comment`).
   */
  kind: 'ask' | 'words' | 'speak'
  /**
   * A words board in his hand: the sheets it was taken with. They stay as they are until he lets it go; a newer answer
   * comes as the next board, as a new ask does. Empty for an ask, and at the Bot (it shows the newest read there).
   */
  sheets: readonly Sheet[]
  /** The sheet of the words or the question it shows (0 for an approval). */
  sheet: number
  /** A question board: the sheets he has had in his hand (`sheet` too), so his note never answers one he did not see. */
  looked: readonly number[]
  /** A words or question board that stands at its Bot in place of an ask board he handed back: that ask's id, else ''. */
  over: string
  /** A question board: the boxes that carry a print, by question (`question.ts`), and each print, by `q|label`. */
  picks: Picks
  marks: Readonly<Record<string, Print>>
  /** His note on the paper (`comment.ts`), and whether the line to write it on is open now. */
  comment: string
  writing: boolean
}

/**
 * The ask that a Bot holds up a board for: its oldest approval with a request; with none, its oldest question that XR
 * can answer (TALK slice 2); else none. `questionsOnly`: past a board he handed back only a question comes (D27).
 */
export function boardAskOf(bot: Pick<XrBot, 'needs'>, questionsOnly = false): XrNeed | null {
  // A Bot's asks come by thread, not by age: the oldest is picked by its time.
  const oldest = (ok: (n: XrNeed) => boolean) => { let o: XrNeed | null = null; for (const n of bot.needs) if (ok(n) && (!o || n.ts < o.ts)) o = n; return o }
  return (questionsOnly ? null : oldest(n => n.kind === 'approval' && !!n.requestId)) ?? oldest(answerable)
}

/** The board's text: one sentence, and the exact command (or the tool's name) under it, whole; for a question, its choices. */
export function boardText(need: XrNeed): { sentence: string; command: string } {
  if (need.kind === 'question') return { sentence: `${need.who}: ${need.action}`, command: (need.questions[0]?.options ?? []).join(' \u00B7 ') }
  return { sentence: `${need.who}: ${need.action || need.tool}`, command: need.command || need.tool }
}

/** A Bot holds up a new board for its ask. */
export const boardFor = (need: XrNeed): Board => ({ needId: need.id, bot: need.bot, version: need.version, where: 'bot', calmFrom: 0, stamped: false, floorAt: 0, responseId: '', noId: '', awaySince: null, back: false, kind: 'ask', sheet: 0, over: '', sheets: [], looked: [], picks: {}, marks: {}, comment: '', writing: false })

/** A Bot holds up its words (`over`: the ask board he handed back that it stands in place of, or ''). */
export const wordsBoard = (says: Pick<Says, 'id' | 'version'>, bot: string, over = ''): Board => ({ ...boardFor({ id: says.id, bot, version: says.version } as XrNeed), kind: 'words', over })

/**
 * The blank sheet a Bot holds up while he holds a pinch on it (P2): `n` makes each hold its own; `words` were kept.
 * Its line opens (`writing`, the recognizer listens) only once the hold has stayed still a little longer (`useBoards`).
 */
export const speakBoard = (bot: string, n: number, words = ''): Board =>
  ({ ...boardFor({ id: `speak:${bot}:${n}`, bot, version: '' } as XrNeed), kind: 'speak', comment: words, writing: false })

/** He takes a words board: it comes into his hand at its first sheet. */
export const takeWords = (b: Board, now: number, says?: Pick<Says, 'version' | 'sheets'>): Board =>
  ({ ...b, where: 'held', calmFrom: now, awaySince: null, back: false, sheet: 0, version: says?.version ?? b.version, sheets: says?.sheets ?? b.sheets , put: false })

/** He takes it: it comes into his hand, bound to the ask as it is now. */
export const takeBoard = (b: Board, need: XrNeed, now: number): Board => ({ ...b, where: 'held', version: need.version, calmFrom: now, awaySince: null, back: false , put: false })

/**
 * A Home line grabs that ask's own board (D5, D24), as the boards are now: the board of the ask when it stands at its
 * Bot; else, for an approval that waits behind another board of the same Bot, that board steps back (its ask waits
 * again) and this ask's board comes into his hand. Only a board at the Bot steps back, not stamped: one in his hand
 * or on the floor stays, and the grab does nothing (`null`). A Bot with no board in the room gives `null` as well.
 */
export function grabAsk(was: ReadonlyMap<string, Board>, need: XrNeed, now: number): Map<string, Board> | null {
  const own = was.get(need.id)
  if (own) return own.where === 'bot' ? new Map(was).set(need.id, takeBoard(own, need, now)) : null
  if (!(need.kind === 'approval' && need.requestId) && !answerable(need)) return null
  const standing = [...was].filter(([, b]) => b.bot === need.bot)
  if (!standing.length || standing.some(([, b]) => b.where !== 'bot' || b.stamped)) return null
  const next = new Map(was)
  for (const [id] of standing) next.delete(id)
  next.set(need.id, takeBoard(boardFor(need), need, now))
  return next
}

/**
 * The asks that come up past a board he handed back (D27): a question, and an ask flagged as waiting with no request
 * yet (`attention`). Only an approval waits behind it, one board at a time.
 */
export const passesHandBack = (kind: string | undefined): boolean => kind === 'question' || kind === 'attention'

/**
 * How many asks of a Bot wait behind its board (D24): all its asks but the board's own, and none a board answered.
 * Behind a board he handed back (`back`, D27) its questions do not wait, so they are not counted.
 */
export const waitingBehind = (bot: Pick<XrBot, 'needs'> | undefined, boardAsk: string, answered: ReadonlySet<string>, back = false): number =>
  bot ? bot.needs.filter(n => n.id !== boardAsk && !answered.has(n.id) && !(back && passesHandBack(n.kind))).length : 0

/** The Bots whose board he handed back himself and that stands so (D27): their questions do not wait behind it. */
export function handedBack(boards: Iterable<Board>): Set<string> {
  const out = new Set<string>()
  const other = new Set<string>()
  for (const b of boards) (b.back && b.where === 'bot' ? out : other).add(b.bot)
  for (const bot of other) out.delete(bot)
  return out
}

/** The asks whose board he handed back and that stands at its Bot so: by the Bot's slug, the ask's id (`places.staysHome`). */
export const handedBackAsks = (boards: Iterable<Board>): Record<string, string> =>
  Object.fromEntries([...boards].filter(b => b.back && b.where === 'bot').map(b => [b.bot, b.needId]))

/** The new boards at a Bot (not in `was`) whose ask is one of `asks`, as handed back. */
export const markBack = (boards: ReadonlyMap<string, Board>, was: ReadonlyMap<string, Board>, asks: ReadonlySet<string>): Board[] =>
  asks.size ? [...boards.values()].filter(b => asks.has(b.needId) && !was.has(b.needId) && b.kind === 'ask' && b.where === 'bot' && !b.back).map(b => ({ ...b, back: true })) : []

/** What a stamp does now. */
export type StampFate =
  | { do: 'answer'; board: Board }
  | { do: 'changed'; board: Board }
  | { do: 'wait' }
  | { do: 'gone' }
  | { do: 'nothing' }

/**
 * A stamp on a held board, with `need` the ask as it is now (`null`: answered elsewhere, or withdrawn). `mint` makes
 * the decision's one id. A stamp on a stamped board does nothing more.
 */
export function stampBoard(b: Board, need: XrNeed | null, now: number, mint: () => string): StampFate {
  if (b.where !== 'held' || b.stamped) return { do: 'nothing' }
  if (!need || need.id !== b.needId) return { do: 'gone' }
  if (need.version !== b.version) return { do: 'changed', board: { ...b, version: need.version, calmFrom: now } }
  if (now - b.calmFrom < BOARD.calmMs) return { do: 'wait' }
  return { do: 'answer', board: { ...b, stamped: true, responseId: b.responseId || mint() } }
}

/** What a stamp on a question board does: a box marked (and the answer, when it is the last one), or as `stampBoard`. */
export type QuestionStamp = StampFate | { do: 'pick'; board: Board } | { do: 'miss' }

/**
 * A stamp on a held question board at box `box` of its sheet (`-1`: on no box), its print at `print`: the box carries
 * the print, and the answer goes once every question has its one choice (`answersAtPrint`, then `answer`).
 */
export function stampQuestion(b: Board, need: XrNeed | null, now: number, box: number, print: Print, mint: () => string): QuestionStamp & { answers?: Record<string, string> } {
  if (b.where !== 'held' || b.stamped) return { do: 'nothing' }
  if (!need || need.id !== b.needId) return { do: 'gone' }
  if (need.version !== b.version) return { do: 'changed', board: { ...b, version: need.version, calmFrom: now, picks: {}, marks: {} } }
  if (now - b.calmFrom < BOARD.calmMs) return { do: 'wait' }
  if (box < 0) return { do: 'miss' }
  const picks = pick(need, b.picks, b.sheet, box)
  if (picks === b.picks) return { do: 'miss' }
  const q = Object.keys(picks).find(k => picks[Number(k)] !== b.picks[Number(k)]) ?? ''
  const label = [...(picks[Number(q)] ?? [])].find(l => !(b.picks[Number(q)] ?? []).includes(l))
  const marks = { ...b.marks }
  // A box that took back its choice loses its print; a box that took one carries it.
  for (const key of Object.keys(marks)) if (key.startsWith(`${q}|`) && !(picks[Number(q)] ?? []).includes(key.slice(q.length + 1))) delete marks[key]
  if (label !== undefined) marks[`${q}|${label}`] = print
  const next = { ...b, picks, marks }
  if (!answersAtPrint(need, picks)) return { do: 'pick', board: next }
  return { do: 'answer', board: { ...next, stamped: true, writing: false, responseId: b.responseId || mint() }, answers: answerOf(need, picks, b.comment, ...sightOf(b, need)) ?? undefined }
}

/** The question on the sheet in his hand, and every question whose sheet he has had in his hand (`looked`). */
export const sightOf = (b: Pick<Board, 'sheet' | 'looked'>, need: Pick<XrNeed, 'questions'>): [number, number[]] =>
  [seenAt(need, b.sheet), [...new Set([...b.looked, b.sheet].map(s => seenAt(need, s)))]]

/** The next sheet of a question board in his hand: the one the corner turns to, which he has now seen. */
export const turnTo = (b: Board, sheet: number): Board => ({ ...b, sheet, looked: b.looked.includes(sheet) ? b.looked : [...b.looked, sheet] })

/**
 * What letting go of a held, unstamped question board does with what he made on it (review M1, D4a): his prints and
 * note are its answer when they answer every question (`answer`); short of that the board goes back to its Bot with
 * them, and `open` is the question that still needs a box (`keep`). Only a board he made nothing on is his no when
 * thrown (`none`: the floor, then dismissed).
 */
export function questionLetGo(b: Board, need: Pick<XrNeed, 'questions'>): { do: 'answer'; answers: Record<string, string> } | { do: 'keep'; open: number } | { do: 'none' } {
  const note = b.comment.trim()
  if (!note && !anyPick(b.picks)) return { do: 'none' }
  const sight = sightOf(b, need)
  const answers = answerOf(need, b.picks, note, ...sight)
  if (answers) return { do: 'answer', answers }
  const to = note ? noteFor(need, b.picks, ...sight) : null
  return { do: 'keep', open: stillOpen(need, b.picks).find(i => i !== to) ?? 0 }
}

/**
 * How a held board was let go. In the preview: `px` from the down, `pxPerMs` over the last `speedMs` of its movement,
 * `lastMs` from its last move to the release. In a session: `handM` from the down and `handMps` over its last 100 ms.
 * `onBot`: the release's ray lies on the board's own Bot.
 */
export interface Moved { px?: number; pxPerMs?: number; lastMs?: number; handM?: number; handMps?: number; onBot?: boolean }

/**
 * Whether an up on a board takes it at its Bot, or picks it up from the floor (DASH-16 D): only the up of the pointer
 * whose down began on this same board, while it stood where it stands now. A down on a tray, the room or another
 * board, and a down made before the board came where it is, take nothing. `downs` is the board's own downs, by
 * pointer id, with where the board stood at each.
 */
export function upTakesBoard(downs: ReadonlyMap<number, BoardWhere>, pointerId: number, where: BoardWhere): boolean {
  return (where === 'bot' || where === 'floor') && downs.get(pointerId) === where
}

/** A release that moved nothing: a held board stays in his hand, and nothing is sent, thrown or handed back. */
export const STAY_MOVED: Moved = { handM: 0, handMps: 0 }

/**
 * What a held board's press that ends by a cancel is let go with (D192, 15:50). His own let-go can come as a cancel (a
 * long pinch, `releaseCancels`), and only then is `pinchRoute.ending` set (`pinchRoute.release`): it is read as his
 * release is. Any other cancel (a lost hand, a hand that unmounts, a pointer's exit, the preview's `pointercancel`) is
 * not his act and lets go as a stay, so his note stays on the board.
 */
export function cancelMoved(letGo: boolean, moved: Moved): Moved {
  return letGo ? moved : STAY_MOVED
}

/**
 * A release of a held board: `stay` when it hardly moved (a click on its body does nothing), a throw when it moved far
 * and was still fast at the release, else a hand-back. A release on its own Bot is a hand-back whatever the distance.
 */
export function releaseKind(at: Moved): 'throw' | 'back' | 'stay' {
  if (at.handM !== undefined) {
    if (at.handM < BOARD.stayM) return 'stay'
    if (at.onBot) return 'back'
    return at.handM >= BOARD.throwM && (at.handMps ?? 0) >= BOARD.throwMps ? 'throw' : 'back'
  }
  if ((at.px ?? 0) < BOARD.stayPx) return 'stay'
  if (at.onBot) return 'back'
  return (at.px ?? 0) >= BOARD.throwPx && (at.pxPerMs ?? 0) >= BOARD.throwPxPerMs && (at.lastMs ?? Infinity) <= BOARD.lastMoveMs ? 'throw' : 'back'
}

type Pt = { x: number; y: number; t: number }

/**
 * How a press in the preview moved, from its down, its moves (by the events' own times) and its up: how far, how fast
 * over the last `speedMs` of its movement, and how long before the up its last move came.
 */
export function previewMoved(down: Pt, moves: readonly Pt[], up: Pt): Pick<Moved, 'px' | 'pxPerMs' | 'lastMs'> {
  const px = Math.hypot(up.x - down.x, up.y - down.y)
  const last = moves.at(-1) ?? down
  const pts = [down, ...moves]
  let from = down
  for (const q of pts) if (last.t - q.t >= BOARD.speedMs) from = q
  const pxPerMs = Math.hypot(last.x - from.x, last.y - from.y) / Math.max(1, last.t - from.t)
  return { px, pxPerMs, lastMs: up.t - last.t }
}

/**
 * In a session, how a held board's press was let go: the hand as it was when its fingers opened (`released`, the
 * pinch's own reading of travel and speed), when that pinch is the press's own hand's; else the press's own samples.
 */
export function sessionMoved(own: { hand: 'left' | 'right' | null; handM: number; handMps: number }, released: { hand: 'left' | 'right'; travelM: number; mps: number } | null): { handM: number; handMps: number } {
  if (released && own.hand && released.hand === own.hand) return { handM: released.travelM, handMps: released.mps }
  return { handM: own.handM, handMps: own.handMps }
}

/** How fast a hand moved over its last `handSpeedMs`, in metres a second, from its places (`t` in ms). */
export function handSpeed(samples: readonly { at: readonly [number, number, number]; t: number }[]): number {
  const last = samples.at(-1)
  if (!last) return 0
  let from = samples[0]
  for (const s of samples) if (last.t - s.t >= BOARD.handSpeedMs) from = s
  const dt = last.t - from.t
  return dt > 0 ? Math.hypot(last.at[0] - from.at[0], last.at[1] - from.at[1], last.at[2] - from.at[2]) / (dt / 1000) : 0
}

/**
 * What a throw does: a stamped board falls apart (the yes went at the stamp); an unstamped one lies on the floor. A
 * board of words has nothing to sign: thrown, it is read and falls apart.
 */
export const throwBoard = (b: Board, now: number): { board: Board | null; particles: boolean } =>
  b.stamped || b.kind === 'words' ? { board: null, particles: true } : { board: { ...b, where: 'floor', floorAt: now }, particles: false }

/** He picks a board up from the floor before its time ends: no answer goes, and it is in his hand again. */
export const pickUp = (b: Board, now: number): Board => (b.where === 'floor' ? { ...b, where: 'held', floorAt: 0, calmFrom: now } : b)

/**
 * It goes back to its Bot, and no answer goes. `his`: he handed it back himself, by his release (D27). A board that
 * goes back by itself (out of his view too long, or its ask changed on the floor) is not marked.
 */
export const handBack = (b: Board, his = false): Board => ({ ...b, where: 'bot', awaySince: null, back: his })

/** The most boards in his hand at once (D75): two stand whole in the lens, a third would not. */
export const MOST_HELD = 2

/**
 * The board that leaves his hand when he holds more than `MOST_HELD`: the one he took first (its `calmFrom` is its
 * take), not the sheet he speaks on. `null` while he holds no more than that. The boards' hook lets it go as his own
 * hand would: an unstamped one is handed back to its Bot and stays asked, a stamped one is filed (`useBoards.ts`).
 */
export function oldestOverHeld(boards: ReadonlyMap<string, Board>, most: number = MOST_HELD): { id: string; stamped: boolean } | null {
  const held = [...boards].filter(([, b]) => b.where === 'held' && b.kind !== 'speak')
  // A board he writes on is never the one that goes (D79 2): the oldest one he is not writing on goes.
  const free = held.filter(([, b]) => !b.writing)
  if (held.length <= most || !free.length) return null
  const [id, b] = free.reduce((a, x) => (x[1].calmFrom < a[1].calmFrom ? x : a))
  return { id, stamped: b.stamped }
}

/** Whether a take now must do nothing (D79 2): he holds `MOST_HELD` boards and writes on every one of them. */
export function heldFull(boards: Iterable<Board>, most: number = MOST_HELD): boolean {
  const held = [...boards].filter(b => b.where === 'held' && b.kind !== 'speak')
  return held.length >= most && held.every(b => b.writing)
}

/** A sheet that a third take put by with his note on it, standing at its Bot. */
const isPutSheet = (b: Board): boolean => b.kind === 'words' && !!b.put && b.where === 'bot'
/** The sheet of a note whose ask was answered elsewhere, standing at its Bot (F2): no ask's board, though it is of the ask kind. */
const isEndedSheet = (b: Board): boolean => !!b.elsewhere && b.where === 'bot'
/** Such a sheet waits behind an ask's board at the same Bot: it is kept, with its note, and not drawn until that board is gone (F1, M56). */
export const waitsBehindAsk = (b: Board, boards: Iterable<Board>): boolean => (isPutSheet(b) || isEndedSheet(b)) && [...boards].some(x => x !== b && x.bot === b.bot && x.kind === 'ask' && !x.elsewhere && x.where === 'bot')

/**
 * A board that leaves his hand because a third came (D79 2): back to its Bot AS IT IS, his prints, marks and words
 * kept on it, and nothing sent. His writing on it ends (no line is open at a Bot); what he wrote stays.
 * It is put by, not handed back (D83 1): `back` would be his "not now", past which the Bot's question and words come
 * and take its place, with an empty board for its ask later.
 */
export const putBackAsIs = (b: Board): Board => ({ ...handBack({ ...b, writing: false }), put: true })

/**
 * The floor's time ended, with `need` the ask as it is now: the no goes now, with its own id, never the yes's. An ask
 * that changed while the board lay there gets no no (he never read its new words), nor one that carries a print: the
 * board goes back to its Bot.
 * `null` while it still lies there, or when the ask is gone.
 */
export function floorEnds(b: Board, need: XrNeed | null, now: number, mint: () => string): { do: 'no'; board: Board } | { do: 'back'; board: Board } | null {
  if (b.where !== 'floor' || now - b.floorAt < BOARD.floorMs || !need) return null
  // A board with a print is never dismissed: it goes back to its Bot with its prints (review M1).
  if (need.version !== b.version || anyPick(b.picks)) return { do: 'back', board: handBack(b) }
  return { do: 'no', board: { ...b, noId: b.noId || mint() } }
}

/** A held board out of his view: after `awayMs` it goes back to its Bot. */
export function awayTick(b: Board, inView: boolean, now: number): Board {
  if (b.where !== 'held') return b
  if (inView) return b.awaySince === null ? b : { ...b, awaySince: null }
  if (b.awaySince === null) return { ...b, awaySince: now }
  return now - b.awaySince >= BOARD.awayMs ? handBack(b) : b
}

/**
 * The boards of the crew now, from the boards there were: a Bot with an approval holds up a board for its oldest; a
 * board whose ask is no longer there ends (the second list names those, for the line that says so), unless its yes or
 * no is on its way. A Bot's board for another ask than its oldest stays while he holds it.
 *
 * `drawn` names the Bots whose body is drawn (all of them when it is left out): only those hold up a new board, and
 * a board at a Bot that is no longer drawn ends with no line (nobody answered it; its ask's panel takes over). A
 * board in his hand or on the floor keeps its ask while the ask is listed, whether or not its Bot is drawn.
 *
 * `words` says what each Bot's clipboard says of its words (`says.ts`). A Bot whose words are raised and that has no
 * board holds them up; an approval of its Bot comes first (T8), so a words board at the Bot steps back quietly for
 * one, and comes again after it. A board he handed back stands aside for the words, as a question comes past it
 * (D27); its ask waits behind the words board (`over`). A words board in his hand stays, with the sheets it was
 * taken with (a newer answer comes as the next board); one at the Bot stays while its words are raised, and follows
 * a newer answer.
 *
 * `speaking`: the sheets of the holds that are still on (P2). A hold's sheet that is not among them goes, so a hold
 * whose end was never heard leaves no sheet that stands in the way of its Bot's next ask (D24).
 */
export function boardsNow(was: ReadonlyMap<string, Board>, bots: readonly Pick<XrBot, 'slug' | 'needs'>[], answered: ReadonlySet<string> = new Set(), drawn?: ReadonlySet<string>, words: ReadonlyMap<string, Says> = new Map(), speaking?: ReadonlySet<string>): { boards: Map<string, Board>; gone: Board[] } {
  const boards = new Map<string, Board>()
  const gone: Board[] = []
  const asks = new Map<string, XrNeed>()
  const shown = (slug: string) => !drawn || drawn.has(slug)
  for (const bot of bots) for (const n of bot.needs) asks.set(n.id, n)
  const approvalOf = (slug: string) => { const bot = bots.find(x => x.slug === slug); return bot ? boardAskOf({ needs: bot.needs.filter(n => !answered.has(n.id)) }) : null }
  /** The ask boards that stand aside for their Bot's words now, by Bot. */
  const aside = new Map<string, string>()
  for (const [id, b] of was) {
    // Its Bot is not drawn now: a board still at it goes quietly.
    if (b.where === 'bot' && !shown(b.bot)) continue
    // The sheet he speaks on stands while he holds it: it is his, and no ask of that Bot comes past it.
    if (b.kind === 'speak') { if (!speaking || speaking.has(id)) boards.set(id, b); continue }
    if (b.kind === 'words') {
      const w = words.get(b.bot)
      if (b.where !== 'bot') { boards.set(id, b); continue }
      // Put by with his note on it: it stands at its Bot, raised or not, until he takes it again.
      if (b.put && b.comment.trim()) { boards.set(id, b); continue }
      const ask = approvalOf(b.bot)
      // At the Bot it stands while its words are raised, of its own session: a Bot holds one session's words at a time.
      // Handed back, it went with his release (`useBoards`).
      if (!w || !w.raise || w.id !== id || (ask && ask.id !== b.over)) continue
      boards.set(id, w.version !== b.version ? { ...b, version: w.version, sheet: 0 } : b)
      continue
    }
    // Past a board he handed back come his Bot's question (as a sheet) and its words (D27).
    const question = b.where === 'bot' && b.back && !b.stamped ? boardAskOf({ needs: (bots.find(x => x.slug === b.bot)?.needs ?? []).filter(n => n.id !== id && !answered.has(n.id)) }, true) : null
    if (b.where === 'bot' && b.back && !b.stamped && !b.elsewhere && (question || words.get(b.bot)?.raise)) { aside.set(b.bot, id); continue }
    // A stamped board stays in his hand after its ask is answered, until he throws it.
    // So does one in his hand that carries his note: he reads it until he lets it go (nothing of it is sent then).
    if (asks.has(id) || (b.where === 'held' && (b.stamped || !!b.comment.trim()))) boards.set(id, b)
    // Its ask ended elsewhere while it stands at its Bot with his unsent note: the note is never dropped in silence
    // (F2). The board stays, as a sheet that says so, until he hands it to its Bot (the note goes) or throws it.
    else if (!b.stamped && b.kind === 'ask' && b.where === 'bot' && b.comment.trim()) boards.set(id, b.elsewhere ? b : { ...b, elsewhere: true })
    // Answered elsewhere, or withdrawn: it ends, and a no that waited on the floor never goes.
    else if (!b.stamped) gone.push(b)
  }
  for (const bot of bots) {
    if (!shown(bot.slug)) continue
    // An ask a board already answered may still be listed until the list is read again: it gets no second board.
    const open = { needs: bot.needs.filter(n => !answered.has(n.id) && n.id !== aside.get(bot.slug)) }
    const need = boardAskOf(open, aside.has(bot.slug))
    if (!need || boards.has(need.id)) continue
    // One board in the room for each Bot: a held one of another ask of it stands in for the next.
    // A sheet put by with his note is no such board: an ask outranks it and shows its own (F1); the sheet waits behind.
    if ([...boards.values()].some(b => b.bot === bot.slug && !isPutSheet(b) && !isEndedSheet(b))) continue
    boards.set(need.id, { ...boardFor(need), over: aside.get(bot.slug) ?? '' })
  }
  for (const bot of bots) {
    const w = words.get(bot.slug)
    if (!w?.raise || !shown(bot.slug) || [...boards.values()].some(b => b.bot === bot.slug)) continue
    boards.set(w.id, wordsBoard(w, bot.slug, aside.get(bot.slug) ?? ''))
  }
  return { boards, gone }
}

/**
 * A sheet's upper edge in his hand, in degrees over level (M42, M43): `clearDeg` under the lower edge of Home's strip
 * as it stands now (`homePlace.strip`, which Home writes from `homeLayout` and its lift), and never over the lens's
 * top at the take less `clearDeg`, so its heading is in the lens when he pinched a Bot that stands under level.
 * `noStripDeg`: with no strip drawn. Fixed at the take: it does not follow his head after.
 */
const SHEET_TOP = { clearDeg: 1, noStripDeg: 16.5 } as const
/** The sheet's top for a take with his eye at `eye`, his look `pitchDeg` over level and the lens `halfDeg` tall each way. */
export function sheetTopDeg(eye: { x: number; y: number; z: number }, pitchDeg: number, halfDeg: number, strip: { center: { x: number; y: number; z: number }; heightM: number } | null): number {
  const low = strip ? (Math.atan2(strip.center.y - strip.heightM / 2 - eye.y, Math.hypot(strip.center.x - eye.x, strip.center.z - eye.z)) * 180) / Math.PI - SHEET_TOP.clearDeg : SHEET_TOP.noStripDeg
  return Math.min(low, pitchDeg + halfDeg - SHEET_TOP.clearDeg)
}

/** His lens at a take: his look's pitch, half the lens's height, and the line under Home's strip (`sheetTopDeg`), in degrees. */
export interface TakeLens { pitchDeg: number; halfDeg: number; lineDeg: number }
/**
 * How far up (metres) an ask's board stands from its own place so that it is whole in the lens at the take (D92 2):
 * its lower edge a degree inside the lens's, then its upper edge a degree inside the lens's and under Home's line.
 * A board taller than the room between them keeps its top: the top wins.
 */
export function askBoardShiftM(heightM: number, distanceM: number, dropM: number, lens: TakeLens): number {
  const at = (deg: number) => Math.tan((Math.max(-80, Math.min(80, deg)) * Math.PI) / 180) * distanceM
  const top = heightM / 2 - dropM
  const bottom = -heightM / 2 - dropM
  const low = at(lens.pitchDeg - lens.halfDeg + SHEET_TOP.clearDeg)
  let shift = bottom < low ? low - bottom : 0
  const high = Math.min(at(lens.pitchDeg + lens.halfDeg - SHEET_TOP.clearDeg), at(lens.lineDeg))
  if (top + shift > high) shift = high - top
  return shift
}

