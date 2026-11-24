/**
 * The boards of the room of pages (`board.ts`): which Bot holds one up now, and what his take, stamp, throw, pick-up
 * and hand-back do. The stamp and the no reach the gateway through `boardDecide` only, each with its own
 * `responseId`, and the line that follows says what the gateway decided, which may be another answer than his.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { mintResponseId } from '../../../components/chat-input/sharedDecision'
import { i18nT } from '../../../i18n/t'
import type { XrBot, XrNeed } from '../model'
import { boardAnswer, boardDecide, cutRetry } from '../writes'
import { printTilt, type Print, type Uv } from './boardPicture'
import { BOARD, awayTick, boardText, boardsNow, floorEnds, grabAsk, handBack, handedBack, handedBackAsks, heldFull, markBack, oldestOverHeld, pickUp, putBackAsIs, releaseKind, speakBoard, stampBoard, stampQuestion, takeBoard, takeWords, questionLetGo, throwBoard, turnTo, waitingBehind, waitsBehindAsk, type Board, type Moved } from './board'
import { questionSheets } from './question'
import { NOTES_KEPT, type KeptNotes } from './room'
import { isWorkingSheet, nextSheet, retryStands, slotOfSays, type Says, type Sheet } from './says'
import { HOLD, speakEnd, type SpeakEnd } from './speak'
import type { SentFrom } from './useComment'
import { chosenOn, keepChosen } from './chosen'
import { fnv1a8 } from '../../../utils/ensureChatFolder'

/** How long a handed-over note waits for its send's answer before it counts as not sent (M85). */
export const NOTE_SEND_WAIT_MS = 20_000

/**
 * The send id of a note on an ask (M85): the same ask and the same words give the same id, in this tab and after a
 * reload; another note on that ask, or the same words on another ask, gives another. Not a secret: a tag.
 */
export const noteSendId = (askId: string, note: string): string => `n-${fnv1a8(askId)}-${fnv1a8(note)}-${fnv1a8(`${askId}\n${note}`)}`

/** A board as it is drawn: its state, its ask as it is now, and its text. */
export interface BoardView extends Board {
  need: XrNeed | null
  label: string
  text: ReturnType<typeof boardText> | null
  print: Print | null
  /** The asks of its Bot that wait behind it (D24), and its words when they wait too: a tag on the clip. */
  behind: number
  /** A question board's question as it was last listed: a stamped board keeps showing its sheet and its prints. */
  asked: XrNeed | null
  /** A words board's sheets (`says.ts`); none for an ask. */
  sheets: readonly Sheet[]
}

/** What the gateway decided; `ended`: a question's wait had ended before his answer, and none of his went. */
type Verdict = 'approved' | 'rejected' | 'ended'

/** What the probe reads: each board, every decision that a board sent, each release, and the lines the boards said. */
export interface BoardProbe {
  boards: { needId: string; bot: string; kind: Board['kind']; ask: string; sheet: number; sheets: number; n: number; of: number; before: boolean; lines: number; lastLine: string; picks: Board['picks']; comment: string; writing: boolean; behind: number; back: boolean; where: Board['where']; stamped: boolean; version: string; responseId: string; noId: string; sentence: string; command: string; print: Print | null }[]
  /** `verdict`: what the gateway said the request was decided as; `null` while on its way or when it failed. */
  sent: { kind: 'approve' | 'reject'; needId: string; requestId: string; responseId: string; ok: boolean | null; verdict: Verdict | null }[]
  /** Each release of a held board: what it was read as, from how it moved. */
  releases: { needId: string; kind: ReturnType<typeof releaseKind>; moved: Moved }[]
  said: string[]
  /** Each hold on a Bot (P2) and how it ended: `sent` with the words that went, else nothing went. */
  holds: { bot: string; id: string; end: SpeakEnd | 'board' | ''; words: string }[]
  /** The boards at a Bot that a nearer page hides now. */
  hidden: string[]
  /** Each "Try again" of a failed sheet that went, by session, in order. */
  retried: string[]
}

export interface BoardHost {
  say: (words: string) => void
  /** Open the Bot's real page (the board's corner mark). */
  openBot: (slug: string) => void
  /** The Bots whose body is drawn (at the desk, the seated ones); all, when left out (`boardsNow`). */
  drawn?: ReadonlySet<string>
  /** What each Bot's clipboard says of its words (`useSays`). */
  words?: ReadonlyMap<string, Says>
  /** He filed a Bot's words by a throw: the answer `version` that the board showed, of session `slot`. */
  read?: (slug: string, version: string, slot: string, cut?: string) => void
  /** He handed a Bot's words back (D35): kept for later. (He read them at the take, D77 4.) */
  keep?: (slug: string, slot: string, cut?: string) => void
  /** He took a Bot's sheet in his hand (D39, ruling 1): he has looked, so the line of what changed since he left lets it go. */
  looked?: (slug: string, took: 'done' | 'failed') => void
  /**
   * His words to a Bot's own thread (`useComment`), once; whether they went: his note at a board's hand-back or throw,
   * or what he said on the sheet of a hold (`from`).
   */
  sendNote?: (slug: string, text: string, from?: SentFrom, id?: string) => Promise<boolean> | undefined
  /** The asks whose board he handed back before a reload (`room.readHome`): each such board stands handed back again. */
  backAtStart?: Readonly<Record<string, string>>
  /** His notes on boards before a reload, and which were put by, by ask id (`room.readNotes`, M45). */
  notesAtStart?: KeptNotes
  /** The crew's asks were read whole (default true): only then is a kept note of an ask that is not listed let go (M49). */
  known?: boolean
  /**
   * The new chat card's speech stands aside (`true`) while a hold's sheet listens (`useChatCard.micAway`). It is told
   * in the same step as the sheet's line opens or goes, so the card's recognizer stops before the hold's starts, and
   * starts again only after the hold's has stopped.
   */
  micAway?: (away: boolean) => void
}

/** A board's answer: an approval's yes or no, or a question's answer (`answers`; none to dismiss it). */
/** Between two paragraphs of a message: a chosen box's words, then his note. */
const PARAGRAPH = '\n\n'

type Decide = (need: XrNeed, kind: 'approve' | 'reject', responseId: string, answers?: Record<string, string>) => Promise<Verdict>
const decideOrAnswer: Decide = (need, kind, responseId, answers) =>
  need.kind === 'question' ? boardAnswer(need, kind === 'approve' ? answers ?? null : null, responseId) : boardDecide(need, kind, responseId)

/** A words board's sheets: in his hand the ones it was taken with, at its Bot the newest read's. */
const sheetsOfBoard = (b: Board, words?: ReadonlyMap<string, Says>): readonly Sheet[] =>
  b.kind !== 'words' ? [] : b.where === 'held' ? b.sheets : words?.get(b.bot)?.sheets ?? []

/** For the probe: how many sheets a words board has, of how many its shown sheet's answer has, and its lines. */
const sheetProbe = (b: Board, words?: ReadonlyMap<string, Says>) => {
  const sheets = sheetsOfBoard(b, words)
  const at = sheets[Math.min(b.sheet, sheets.length - 1)]
  return { sheets: sheets.length, n: at?.n ?? 0, of: at?.of ?? 0, before: !!at?.before, lines: at ? at.head.length + at.body.length : 0, lastLine: at?.body.at(-1) ?? at?.head.at(-1) ?? '' }
}

/** What waits behind a board, for its tag: its Bot's other asks (D24, D27), and its Bot's words behind an ask board. */
const behindOf = (b: Board, crew: readonly XrBot[], answered: ReadonlySet<string>, words?: ReadonlyMap<string, Says>) =>
  // A words board at its Bot holds no ask but the one it stands in place of (`over`).
  b.kind === 'speak' ? 0 : b.kind === 'words' && b.where === 'bot' ? (b.over ? 1 : 0) : waitingBehind(crew.find(x => x.slug === b.bot), b.needId, answered, b.back) + (b.kind === 'ask' && words?.get(b.bot)?.raise ? 1 : 0)

/** The cut that a failed sheet names, read off the board itself; none for any other board. */
const failedCut = (b: Board): string | undefined => (b.kind === 'words' ? b.sheets.find(s => s.done?.failed)?.done?.failed?.cut : undefined)

export function useBoards(crew: readonly XrBot[], host: BoardHost, decide: Decide = decideOrAnswer) {
  const [boards, setBoards] = useState<ReadonlyMap<string, Board>>(() => new Map())
  const now = useRef(boards)
  now.current = boards
  const hostNow = useRef(host)
  const backLeft = useRef(new Set(Object.values(host.backAtStart ?? {})))
  /**
   * His unsent notes on ask boards, and which boards are put by, by ask id (M45): what the room's record keeps. It
   * outlives its board: a board that goes while its Bot is not drawn, or over a reload, gets its note again when it
   * comes back. A note leaves the book when its board is seen without it (sent, or wiped by him) or stamped.
   */
  const noteBook = useRef(new Map(Object.entries(host.notesAtStart ?? {})))
  const [bookRev, setBookRev] = useState(0)
  /** The ended notes that he handed over and whose send has not answered yet (M64), by ask id. */
  const sendingNotes = useRef(new Set<string>())
  hostNow.current = host
  const crewNow = useRef(crew)
  crewNow.current = crew
  const sent = useRef<BoardProbe['sent']>([])
  const releases = useRef<BoardProbe['releases']>([])
  const said = useRef<string[]>([])
  /** Where each stamped board's print is (D17: where the stamp was pressed). */
  const [prints, setPrints] = useState<ReadonlyMap<string, Print>>(() => new Map())
  const printsNow = useRef(prints)
  printsNow.current = prints
  /** The asks a board answered or is answering: one still listed until the list is read again gets no second board. */
  const answered = useRef(new Set<string>())
  const say = useCallback((words: string) => { said.current.push(words); hostNow.current.say(words) }, [])
  /** The holds on Bots, in order (P2), and the words of a hold whose send failed, by Bot: the next hold starts with them. */
  const holds = useRef<BoardProbe['holds']>([])
  const keptWords = useRef(new Map<string, string>())
  /** Each "Try again" that went, by session (slice 2): the probe reads it, so a row sees one and no second. */
  const cutRetried = useRef<string[]>([])
  /** Whether the card's speech stands aside for a hold's line now: told once each way (`BoardHost.micAway`). */
  const micHeld = useRef(false)
  const micAway = useCallback((away: boolean) => { if (micHeld.current === away) return; micHeld.current = away; hostNow.current.micAway?.(away) }, [])
  const needOf = useCallback((id: string): XrNeed | null => {
    for (const bot of crewNow.current) for (const n of bot.needs) if (n.id === id) return n
    return null
  }, [])
  const nameOf = useCallback((slug: string) => crewNow.current.find(b => b.slug === slug)?.label ?? slug, [])
  /** A board changed. `now` follows at once, so a second press before the next render reads this one. */
  const put = useCallback((b: Board | null, id: string) => {
    const next = new Map(now.current)
    if (b) next.set(id, b); else next.delete(id)
    now.current = next
    setBoards(next)
  }, [])
  /** One decision, logged for the probe: the verdict the gateway gave, or `null` when it did not go. */
  const send = useCallback((need: XrNeed, kind: 'approve' | 'reject', responseId: string, answers?: Record<string, string>): Promise<Verdict | null> => {
    const row: BoardProbe['sent'][number] = { kind, needId: need.id, requestId: need.requestId, responseId, ok: null, verdict: null }
    sent.current.push(row)
    // From the send on, the ask gets no new board; a decision that did not reach the gateway gives it back.
    answered.current.add(need.id)
    return decide(need, kind, responseId, answers).then(
      verdict => { row.ok = true; row.verdict = verdict; return verdict },
      () => { row.ok = false; answered.current.delete(need.id); setBoards(new Map(now.current)); return null },
    )
  }, [decide])

  // The crew or the boards changed: a Bot with an approval holds up a board (the next one when a board ends); a
  // board whose ask went ends with one line. An answered ask that left the list is forgotten, so the same id later
  // is a new ask.
  useEffect(() => {
    const listed = new Set(crew.flatMap(b => b.needs.map(n => n.id)))
    for (const id of answered.current) if (!listed.has(id) && !now.current.has(id)) answered.current.delete(id)
    const speaking = new Set(holds.current.filter(h => h.id && !h.end).map(h => h.id))
    const { boards: next, gone } = boardsNow(now.current, crew, answered.current, hostNow.current.drawn, hostNow.current.words, speaking)
    // A board that he handed back before a reload comes up handed back, once: a question still comes past it (D27).
    for (const b of markBack(next, now.current, backLeft.current)) { next.set(b.needId, b); backLeft.current.delete(b.needId) }
    // A board that carried his note before a reload comes up with it, once, put by if it was (M45).
    const bookWas = JSON.stringify([...noteBook.current])
    for (const [id, b] of next) {
      if (b.kind !== 'ask') continue
      const kept = noteBook.current.get(id)
      // A board that is new here (made now, after a reload or after its Bot was not drawn) takes its kept note.
      // A note whose ask ended is no ask's any more (M55): an ask that comes again with that id gets none of it.
      if (!now.current.has(id)) { if (kept && !kept.ended && !b.stamped && !b.comment) next.set(id, { ...b, comment: kept.note, put: kept.put && b.where === 'bot' }); continue }
      // The sheet of a note whose ask ended (F2) keeps its entry, marked as ended, until he hands it over or throws it (M55).
      // An ask that came again with an ended note's id: that note waits in the book, untouched by this board.
      if (kept?.ended && !b.elsewhere) continue
      noteBook.current.delete(id)
      if (b.elsewhere) { if (b.comment.trim()) noteBook.current.set(id, { note: b.comment, put: false, bot: b.bot, ended: true }) }
      else if (!b.stamped && (b.comment.trim() || b.put)) noteBook.current.set(id, { note: b.comment, put: !!b.put && b.where === 'bot', bot: b.bot })
    }
    // A kept note whose ask no Bot lists any more (M49, M55): it stays in the book, marked as ended, so an ask that
    // comes again with that id gets no old note; and whenever no sheet of it stands (after a reload, when its Bot is
    // drawn again, after it stepped aside) one is made at its Bot that says its ask was answered elsewhere (F2). It
    // leaves the book only by his hand-over or his throw (`release`). Only when the crew's asks are known whole.
    if (hostNow.current.known !== false) {
      for (const [id, kept] of [...noteBook.current]) {
        // A note on its way to its Bot (M64) has no sheet: one is made again only when the send says it did not go.
        if (next.has(id) || (listed.has(id) && !kept.ended) || sendingNotes.current.has(id)) continue
        if (!kept.note.trim()) { noteBook.current.delete(id); continue }
        if (!kept.ended) noteBook.current.set(id, { ...kept, put: false, ended: true })
        const bot = kept.bot
        // shortcut: an entry with no Bot (a record of an older build) or of a Bot not in the crew waits in the book unseen; show it at the room's front when such records are met.
        if (listed.has(id) || !bot || !crew.some(c => c.slug === bot) || (hostNow.current.drawn && !hostNow.current.drawn.has(bot))) continue
        next.set(id, { needId: id, bot, version: '', where: 'bot', calmFrom: 0, stamped: false, floorAt: 0, responseId: '', noId: '', awaySince: null, back: false, kind: 'ask', sheet: 0, over: '', sheets: [], looked: [], picks: {}, marks: {}, comment: kept.note, writing: false, elsewhere: true })
      }
    }
    if (JSON.stringify([...noteBook.current]) !== bookWas) setBookRev(n => n + 1)
    // A board whose stamp went but whose reply was lost: its own yes may be what answered it, so the line names no one.
    for (const b of gone) if (b.where === 'held') say(i18nT(b.responseId ? 'xr.board_answered' : 'xr.board_elsewhere', { name: nameOf(b.bot) }))
    const same = next.size === now.current.size && [...next].every(([id, b]) => now.current.get(id) === b)
    if (!same) { now.current = next; setBoards(next) }
    // A hold's line that went by itself (its sheet dropped here): the card's speech comes back in this same step.
    if (![...next.values()].some(b => b.kind === 'speak' && b.writing)) micAway(false)
    // A print goes with its board.
    setPrints(was => ([...was.keys()].every(id => next.has(id)) ? was : new Map([...was].filter(([id]) => next.has(id)))))
  }, [crew, boards, nameOf, say, host.drawn, host.words, micAway])

  // A board on the floor: when its time ends the no goes, once, with its own id, and the board is gone; an ask that
  // changed meanwhile gets none, and its board goes back to its Bot.
  useEffect(() => {
    const timer = setInterval(() => {
      const t = performance.now()
      for (const [id, b] of now.current) {
        const need = needOf(id)
        const due = floorEnds(b, need, t, mintResponseId)
        if (!due || !need) continue
        if (due.do === 'back') { put(due.board, id); say(i18nT('xr.board_changed', { name: nameOf(b.bot) })); continue }
        put(null, id)
        void send(need, 'reject', due.board.noId).then(verdict => {
          // A no that did not go: the ask still waits, and its Bot holds its board up again.
          if (!verdict) { say(i18nT('xr.board_no_failed', { name: nameOf(b.bot) })); return }
          if (verdict === 'ended') { say(i18nT('xr.board_elsewhere', { name: nameOf(b.bot) })); return }
          if (need.kind === 'question') { say(i18nT('xr.question_dismissed', { name: nameOf(b.bot) })); return }
          say(i18nT(verdict === 'rejected' ? 'xr.board_no' : 'xr.board_was_yes', { name: nameOf(b.bot) }))
        })
      }
    }, 250)
    return () => clearInterval(timer)
  }, [needOf, put, send, nameOf, say])

  const take = useCallback((id: string) => {
    const b = now.current.get(id)
    // The sheet of a hold is in his hold, not to be taken.
    if (b?.kind === 'speak') return false
    // He writes on both boards in his hand: a third take does nothing (D79 2).
    if (heldFull(now.current.values())) return false
    if (b?.kind === 'words' && b.where === 'bot') {
      put(takeWords(b, performance.now(), hostNow.current.words?.get(b.bot)), id)
      hostNow.current.looked?.(b.bot, failedCut(b) ? 'failed' : 'done')
      // Taken, a failed or a finished sheet is read (D77 4): Home's counts and names follow at once. What a working
      // Bot does now is no answer to read.
      if (!isWorkingSheet(b.version)) hostNow.current.read?.(b.bot, b.version, slotOfSays(id), failedCut(b))
      return true
    }
    const need = needOf(id)
    // A board that only keeps his note has no ask any more: it is taken as it is.
    if (b?.elsewhere && b.where === 'bot') { put({ ...b, where: 'held', calmFrom: performance.now(), awaySince: null, back: false, put: false }, id); return true }
    if (!b || !need || b.where !== 'bot') return false
    put(takeBoard(b, need, performance.now()), id)
    return true
  }, [needOf, put])

  /**
   * A Home line grabs that ask's own board (D5, D24): its board when it stands at its Bot, or, for an approval that
   * waits behind another board of the Bot at the Bot, that one steps back and this one comes into his hand.
   */
  const grabNow = useCallback((id: string) => {
    const need = needOf(id)
    return need ? grabAsk(now.current, need, performance.now()) : null
  }, [needOf])
  const canGrab = useCallback((id: string) => grabNow(id) !== null, [grabNow])
  const grab = useCallback((id: string) => {
    if (heldFull(now.current.values())) return false
    const next = grabNow(id)
    if (!next) return false
    now.current = next
    setBoards(next)
    return true
  }, [grabNow])

  /**
   * A stamp pressed on a question board at `at`, on box `box` of its sheet (`-1`: on no box): the box carries the print,
   * and the answer goes once every question has its choice (`stampQuestion`).
   */
  const stampBox = useCallback((id: string, at: Uv, box: number) => {
    const b = now.current.get(id)
    if (!b) return 'nothing'
    // A choice that a Bot's words offer him (`[OPTIONS: …]`): the print on its box sends that choice to the Bot, as
    // a press on the choice does on the dashboard; his note goes with it.
    if (b.kind === 'words') {
      const label = b.sheets[b.sheet]?.choices?.[box]
      if (b.where !== 'held' || b.stamped || chosenOn(b.bot, b.version)) return 'nothing'
      // A failed sheet whose cut is gone sends nothing: the Bot runs again already (D39, ruling 5).
      const failed = b.sheets[b.sheet]?.done?.failed
      if (failed && label !== undefined && !retryStands(failed.cut, crewNow.current.find(x => x.slug === b.bot))) { say(i18nT('xr.fail_running_again', { name: nameOf(b.bot) })); return 'nothing' }
      if (performance.now() - b.calmFrom < BOARD.calmMs) { say(i18nT('xr.board_wait')); return 'wait' }
      if (label === undefined) { say(i18nT('xr.question_box')); return 'miss' }
      const note = b.comment.trim()
      const print = { u: at.u, v: at.v, tilt: printTilt(Math.random()), seed: Math.floor(Math.random() * 1e9) }
      put({ ...b, stamped: true, writing: false, picks: { 0: [label] }, marks: { [`0|${label}`]: print } }, id)
      // A failed sheet's one box, "Try again" (P3 slice 2): the cut turn of that session is picked up again, once.
      // The print is kept by the cut's own id, so a second print takes nothing; a reply that does not come sends
      // nothing more either, and the line says to look at the Bot's page before trying again.
      if (b.sheets[b.sheet]?.done?.failed) {
        keepChosen(b.bot, b.version, { label, print })
        const slot = slotOfSays(id)
        cutRetried.current.push(slot)
        void cutRetry(slot).then(
          () => say(i18nT('xr.fail_retry_sent', { name: nameOf(b.bot) })),
          () => say(i18nT('xr.fail_retry_unknown', { name: nameOf(b.bot) })),
        )
        return 'answer'
      }
      // Kept by the answer's own key, so the same words raised again, after a reload too, take no second print.
      keepChosen(b.bot, b.version, { label, print })
      void hostNow.current.sendNote?.(b.bot, [label, note].filter(Boolean).join(PARAGRAPH), 'choice')?.then(went => {
        if (went) { const was = now.current.get(id); if (was) put({ ...was, comment: '' }, id); return }
        // Not sent: the choice can be printed again, and his note stays on the paper.
        keepChosen(b.bot, b.version, null)
        const was = now.current.get(id)
        if (was) put({ ...was, stamped: false, picks: {}, marks: {} }, id)
      })
      return 'answer'
    }
    const need = needOf(id)
    const fate = stampQuestion(b, need, performance.now(), box, { u: at.u, v: at.v, tilt: printTilt(Math.random()), seed: Math.floor(Math.random() * 1e9) }, mintResponseId)
    if (fate.do === 'gone') { put(null, id); say(i18nT('xr.board_elsewhere', { name: nameOf(b.bot) })) }
    else if (fate.do === 'changed') { put(fate.board, id); say(i18nT('xr.board_changed', { name: nameOf(b.bot) })) }
    else if (fate.do === 'wait') say(i18nT('xr.board_wait'))
    else if (fate.do === 'miss') say(i18nT('xr.question_box'))
    else if (fate.do === 'pick') put(fate.board, id)
    else if (fate.do === 'answer' && need) {
      put(fate.board, id)
      void send(need, 'approve', fate.board.responseId, fate.answers).then(verdict => {
        const was = now.current.get(id)
        if (verdict === 'approved' || verdict === 'rejected') { say(i18nT('xr.question_sent', { name: nameOf(b.bot) })); return }
        // Not sent: the board takes a stamp again; its last print goes, and the boxes before it keep theirs. Its note
        // stays on the paper, also when the ask had ended elsewhere.
        if (was) put({ ...was, stamped: false, picks: b.picks, marks: b.marks }, id)
        say(verdict === 'ended' ? i18nT('xr.board_elsewhere', { name: nameOf(b.bot) }) : i18nT('xr.board_failed'))
      })
    }
    return fate.do
  }, [needOf, put, send, nameOf, say])

  /** A stamp pressed on a board at `at` (the picture's fractions): the print shows there when the press answers. */
  const stamp = useCallback((id: string, at?: Uv) => {
    const b = now.current.get(id)
    if (!b) return 'nothing'
    const need = needOf(id)
    const fate = stampBoard(b, need, performance.now(), mintResponseId)
    if (fate.do === 'answer') {
      const seed = Math.floor(Math.random() * 1e9)
      setPrints(was => new Map(was).set(id, { u: at?.u ?? 0.72, v: at?.v ?? 0.6, tilt: printTilt(Math.random()), seed }))
    }
    if (fate.do === 'gone') { put(null, id); say(i18nT('xr.board_elsewhere', { name: nameOf(b.bot) })) }
    else if (fate.do === 'changed') { put(fate.board, id); say(i18nT('xr.board_changed', { name: nameOf(b.bot) })) }
    // A press inside the first second in his hand: the stamp goes back, and one short line says why.
    else if (fate.do === 'wait') say(i18nT('xr.board_wait'))
    else if (fate.do === 'answer' && need) {
      put(fate.board, id)
      void send(need, 'approve', fate.board.responseId).then(verdict => {
        if (verdict === 'approved') return
        const was = now.current.get(id)
        // Decided as a no before his stamp (someone else answered first): the board ends, and the line says so.
        if (verdict === 'rejected') { if (was) put(null, id); say(i18nT('xr.board_was_no', { name: nameOf(b.bot) })); return }
        // Not sent: the board takes a stamp again, and the retry reuses the same id; its print goes with the yes.
        if (was) put({ ...was, stamped: false }, id)
        setPrints(p => { const next = new Map(p); next.delete(id); return next })
        say(i18nT('xr.board_failed'))
      })
    }
    return fate.do
  }, [needOf, put, send, nameOf, say])

  /** A held board let go: thrown, handed back, or (a click with no move) left in his hand. Returns whether it falls apart. */
  const release = useCallback((id: string, moved: Moved) => {
    const b = now.current.get(id)
    if (!b || b.where !== 'held') return { kind: 'none' as const, particles: false }
    const kind = releaseKind(moved)
    releases.current.push({ needId: id, kind, moved })
    if (kind === 'stay') return { kind, particles: false }
    const need = needOf(id)
    const note = b.comment.trim()
    const left = { ...b, writing: false }
    // A question board with his note or his marks on boxes that take several: they are its answer, and it goes now.
    // Short of a whole answer, a board he printed or wrote on goes back to its Bot with all of it, thrown or not: only a
    // throw of a board he made nothing on is his no (review M1, D4a).
    if (need?.kind === 'question' && !b.stamped) {
      const fate = questionLetGo(b, need)
      if (fate.do === 'answer') {
        const done = { ...left, stamped: true, responseId: b.responseId || mintResponseId() }
        put(kind === 'throw' ? null : handBack(done), id)
        void send(need, 'approve', done.responseId, fate.answers).then(verdict => {
          if (verdict === 'approved' || verdict === 'rejected') { say(i18nT('xr.question_sent', { name: nameOf(b.bot) })); return }
          // Not sent, or the ask had ended elsewhere: the board is at its Bot again with what he made on it.
          put(handBack({ ...left, responseId: done.responseId }, true), id)
          say(verdict === 'ended' ? i18nT('xr.board_elsewhere', { name: nameOf(b.bot) }) : i18nT('xr.board_failed'))
        })
        return { kind, particles: kind === 'throw' }
      }
      if (fate.do === 'keep') {
        put(handBack(left, true), id)
        say(i18nT('xr.question_open', { question: need.questions[fate.open]?.question ?? '' }))
        return { kind, particles: false }
      }
    }
    // His note on any other board goes to its Bot's own thread, at the hand-back or the throw (D4, D18).
    // An ask that has left the list (answered elsewhere) takes no note: nothing goes to the thread, and the line says so.
    else if (b.kind === 'ask' && !need && !b.stamped) {
      // With his note on it (F2): handed back, the note goes to its Bot's thread, and comes back onto the board if it
      // did not go; thrown, the board and the note are gone, by his own act.
      if (note && kind === 'back') {
        // The note stays in the book until the send says it went (M55, M64): a reload while it is under way still has
        // it. No send at all, a refusal and a send that says no all count as not sent: the sheet is at its Bot again.
        sendingNotes.current.add(id)
        put(null, id)
        let ended = false
        const end = (went: boolean, told: boolean) => {
          // One end for one hand-over: an answer that comes after the wait is over changes nothing.
          if (ended) return
          ended = true
          clearTimeout(waited)
          sendingNotes.current.delete(id)
          if (went) { if (noteBook.current.delete(id)) setBookRev(n => n + 1); return }
          put({ ...left, where: 'bot', elsewhere: true }, id)
          // The send itself says when it failed (`useComment.send`); with no send, or one that threw, this line does.
          if (!told) say(i18nT('xr.note_failed'))
        }
        // The Bot is told what the note was written on (M57): one fixed line, then his note.
        // A send with no answer in this time counts as not sent (M85): the sheet comes back and he is told.
        const waited = setTimeout(() => end(false, false), NOTE_SEND_WAIT_MS)
        // One note is one request (M85): the send's id is made from the ask and the note's words, so a second
        // hand-over of the same note, also after a reload, is the same message to the gateway (`writes.cardSend`).
        const sent = hostNow.current.sendNote?.(b.bot, [i18nT('xr.note_elsewhere_lead'), note].join('\n'), 'note', noteSendId(id, note))
        if (sent) void sent.then(went => end(went === true, true), () => end(false, false))
        else end(false, false)
        return { kind, particles: false }
      }
      // Thrown, or with no note: by his own act the note leaves the book.
      if (noteBook.current.delete(id)) setBookRev(n => n + 1)
      put(null, id)
      say(i18nT('xr.board_elsewhere', { name: nameOf(b.bot) }))
      return { kind, particles: kind === 'throw' }
    }
    else if (note) {
      left.comment = ''
      // Not sent: his note is on the board again, at its Bot (a words board too, which the hand-back put away).
      void hostNow.current.sendNote?.(b.bot, note)?.then(went => { if (!went) { const was = now.current.get(id); put(handBack({ ...(was ?? left), comment: note }, true), id) } })
    }
    // Words send nothing else either way (T7). Thrown, they are filed: read up to that answer and no further. Handed
    // back, they are kept for later (D35; read at the take, D77 4): the Bot lowers its clipboard, and a tap or Home's line
    // raises them again.
    if (b.kind === 'words') {
      // Handed back, it goes back onto its Bot's clipboard as an ask board does, and is lowered there a beat later
      // (`BoardLower`): never particles, which are a throw's.
      if (kind === 'back') {
        put(handBack(left, true), id)
        const bot = b.bot
        // The cut of the sheet that he let go, if it was a failed sheet: never what the clipboard says now (P3 M2).
        const cut = failedCut(b)
        setTimeout(() => { hostNow.current.keep?.(bot, slotOfSays(id), cut); if (now.current.get(id)?.where === 'bot') put(null, id) }, BOARD.lowerAfterMs)
        return { kind, particles: false }
      }
      hostNow.current.read?.(b.bot, b.version, slotOfSays(id), failedCut(b))
    }
    // His own hand-back: "not now" for this ask, so its Bot's questions no longer wait behind it (D27).
    if (kind === 'back') { put(handBack(left, true), id); return { kind, particles: false } }
    const thrown = throwBoard(left, performance.now())
    put(thrown.board, id)
    if (thrown.board) say(i18nT(need?.kind === 'question' ? 'xr.question_floor' : 'xr.board_floor'))
    return { kind, particles: thrown.particles }
  }, [needOf, put, say, send, nameOf])
  const releaseNow = useRef(release)
  releaseNow.current = release

  // At most two boards in his hand (D75): a third take puts the oldest one he is not writing on back to its Bot as it
  // is, and sends nothing (D79 2): not through `release`, which is his own hand's and answers or sends. A words board
  // is lowered at its Bot a beat later, as his own hand-back of it is.
  useEffect(() => {
    const over = oldestOverHeld(now.current)
    const b = over ? now.current.get(over.id) : undefined
    if (!over || !b) return
    // A stamped board is answered already: put back, it is filed as a throw files it, which sends his finished note
    // (D83 1). (A board he still writes on is never the one that goes: `oldestOverHeld`.)
    if (b.stamped && b.kind === 'ask') { releaseNow.current(over.id, { handM: 1, handMps: 10, onBot: false }); return }
    put(putBackAsIs(b), over.id)
    // A sheet with his note on it stays at its Bot with the note; without one it is lowered as a hand-back is.
    if (b.kind !== 'words' || b.comment.trim()) return
    const cut = failedCut(b)
    setTimeout(() => { hostNow.current.keep?.(b.bot, slotOfSays(over.id), cut); if (now.current.get(over.id)?.where === 'bot') put(null, over.id) }, BOARD.lowerAfterMs)
  }, [boards, put])

  /** A pinch on a held board's foot: the line to write his note on opens there, or closes (`useComment` writes it). */
  const foot = useCallback((id: string) => {
    for (const [other, b] of now.current) if (other !== id && b.writing) put({ ...b, writing: false }, other)
    const b = now.current.get(id)
    if (b?.where === 'held') put({ ...b, writing: !b.writing }, id)
  }, [put])
  /** His note on a board, as it is written (`useComment`). */
  // A note past what the room's record keeps is refused at the keys: the letters do not come, and the line says why (M49).
  const note = useCallback((id: string, text: string) => {
    const b = now.current.get(id)
    if (!b || b.comment === text) return
    if (text.length > NOTES_KEPT.chars && text.length > b.comment.length) { say(i18nT('xr.note_full', { count: NOTES_KEPT.chars })); return }
    put({ ...b, comment: text }, id)
  }, [put, say])

  const pick = useCallback((id: string) => { const b = now.current.get(id); if (b?.where === 'floor') put(pickUp(b, performance.now()), id) }, [put])
  /** The turned corner of a words or a question board: the next sheet (T5). */
  const corner = useCallback((id: string) => {
    const b = now.current.get(id)
    if (!b) return
    const need = needOf(id)
    const count = need?.kind === 'question' ? questionSheets(need).length : sheetsOfBoard(b, hostNow.current.words).length
    if (count > 1) put(turnTo(b, nextSheet(b.sheet, count)), id)
  }, [put, needOf])
  /** The mark on the last sheet of an answer that left something out: the Bot's real page (T6, T11). */
  /** The foot or mark that opens a sheet's Bot's page: the page comes beside, and the sheet in his hand is put by at its Bot (D88 5, M50). */
  const page = useCallback((id: string) => {
    const b = now.current.get(id)
    if (b?.kind !== 'words') return
    hostNow.current.openBot(b.bot)
    // The sheet in his hand goes back to its Bot as it is, as a third take puts one by (M50): never through `release`,
    // which would send a half-written note. With his note it stays at its Bot; with none it is lowered.
    if (b.where !== 'held') return
    put(putBackAsIs(b), id)
    if (b.comment.trim()) return
    const cut = failedCut(b)
    setTimeout(() => { hostNow.current.keep?.(b.bot, slotOfSays(id), cut); if (now.current.get(id)?.where === 'bot') put(null, id) }, BOARD.lowerAfterMs)
  }, [put])
  const away = useCallback((id: string, inView: boolean) => {
    const b = now.current.get(id)
    if (!b) return
    const next = awayTick(b, inView, performance.now())
    if (next !== b) put(next, id)
  }, [put])

  /**
   * He holds a pinch on a Bot, still (P2, Crew's `onHold`): it holds up a blank sheet, and his words go onto it as he
   * speaks (the note's own line: `useComment`). A Bot that holds an ask's board up has it in the way (D24): nothing
   * comes up, and one line says to answer it first. A words board at the Bot stands aside while he speaks.
   */
  const holdStart = useCallback((slug: string) => {
    // One hold at a time (one pinch owns the press): a hold still open is cut off, its words kept for its Bot.
    for (const h of holds.current) {
      if (!h.id || h.end) continue
      const open = now.current.get(h.id)
      h.end = 'cut'
      h.words = (open?.comment ?? '').trim()
      if (h.words) keptWords.current.set(h.bot, h.words)
      put(null, h.id)
      micAway(false)
    }
    const mine = [...now.current.values()].filter(b => b.bot === slug)
    if (mine.some(b => b.kind === 'ask' || b.where !== 'bot')) { holds.current.push({ bot: slug, id: '', end: 'board', words: '' }); say(i18nT('xr.speak_board', { name: nameOf(slug) })); return }
    for (const b of mine) if (b.kind === 'words' || b.kind === 'speak') put(null, b.needId)
    const sheet = speakBoard(slug, holds.current.length, keptWords.current.get(slug) ?? '')
    keptWords.current.delete(slug)
    const row: BoardProbe['holds'][number] = { bot: slug, id: sheet.needId, end: '', words: '' }
    holds.current.push(row)
    put(sheet, sheet.needId)
    // Still a little longer, and the line opens: the recognizer listens only now, so a drag never opened it.
    setTimeout(() => {
      const open = now.current.get(sheet.needId)
      if (!open || row.end) return
      // The card's speech stands aside first, in this same step: its recognizer stops before the hold's starts.
      micAway(true)
      put({ ...open, writing: true }, sheet.needId)
    }, HOLD.listenAfterMs)
  }, [put, say, nameOf, micAway])
  /**
   * The hold ends (`how`): his release sends the words the sheet shows (`heard`: those still being heard, written
   * after its note), once, to that Bot; a hold that moved became the drag, and one that was cut off sends nothing.
   * The sheet goes down either way. Silence leaves no line; spoken words that did not go say so and wait for the next
   * hold on that Bot. Returns whether his words went (the Bot nods then).
   */
  const holdEnd = useCallback((slug: string, how: 'let' | 'drag' | 'cancel', heard = ''): boolean => {
    const row = [...holds.current].reverse().find(h => h.bot === slug && h.id && !h.end)
    const b = row ? now.current.get(row.id) : undefined
    if (!row || !b) return false
    // Before its line opened nothing was heard: what is still being heard belongs to another line.
    const end = speakEnd(how, b.comment, b.writing ? heard : '')
    row.end = end.end
    row.words = end.words
    // The sheet goes, and in the same step the card's speech comes back: it starts after the hold's has stopped.
    put(null, b.needId)
    micAway(false)
    if (end.end === 'sent') {
      void hostNow.current.sendNote?.(slug, end.words, 'speak')?.then(went => { if (!went) keptWords.current.set(slug, end.words) })
      return true
    }
    if (!end.words) return false
    keptWords.current.set(slug, end.words)
    say(i18nT('xr.speak_kept', { name: nameOf(slug) }))
    return false
  }, [put, say, nameOf, micAway])

  /** The last text of each board: a stamped board keeps showing what it allowed after its ask is answered. */
  const texts = useRef(new Map<string, ReturnType<typeof boardText>>())
  const questions = useRef(new Map<string, XrNeed>())
  const views = useMemo<BoardView[]>(() => { for (const id of questions.current.keys()) if (!boards.has(id)) questions.current.delete(id); return [...boards.values()].filter(b => !waitsBehindAsk(b, boards.values())).map(b => {
    const need = needOf(b.needId)
    if (need) texts.current.set(b.needId, boardText(need))
    // A board that only keeps his note says what became of its ask, in place of the ask's words (F2).
    else if (b.kind === 'ask' && (b.elsewhere || (b.where === 'held' && !b.stamped && b.comment.trim()))) texts.current.set(b.needId, { sentence: i18nT('xr.note_elsewhere', { name: nameOf(b.bot) }), command: '' })
    if (need?.kind === 'question') questions.current.set(b.needId, need)
    const w = host.words?.get(b.bot)
    // In his hand a words board keeps what it was taken with.
    if (b.kind === 'words' && w && b.where === 'bot') texts.current.set(b.needId, { sentence: w.sentence, command: '' })
    const behind = behindOf(b, crew, answered.current, host.words)
    // Words he printed a choice on show that print when they are raised again.
    const was = b.kind === 'words' && !b.stamped ? chosenOn(b.bot, b.version) : null
    const shown = was ? { ...b, stamped: true, picks: { 0: [was.label] }, marks: { [`0|${was.label}`]: was.print } } : b
    return { ...shown, need, label: nameOf(b.bot), text: texts.current.get(b.needId) ?? null, print: b.stamped ? prints.get(b.needId) ?? null : null, behind, sheets: sheetsOfBoard(b, host.words), asked: questions.current.get(b.needId) ?? null }
    // `crew` is read here, and stands for what `needOf` and `nameOf` read.
  }) }, [boards, crew, needOf, nameOf, prints, host.words])
  /** The Bots that hold a board now (at a seat the board stands in place of the pad's blank one). */
  // A words board at its Bot holds no ask: an approval takes its place (T8), a question comes up as before.
  // Nor does the sheet he speaks on.
  const holders = useMemo(() => new Set(views.filter(v => !(v.kind === 'words' && v.where === 'bot') && v.kind !== 'speak').map(v => v.bot)), [views])
  /** The Bots whose only board he handed back himself (D27): their questions come up past it. */
  const backBots = useMemo(() => handedBack(boards.values()), [boards])
  const backAsks = useMemo(() => handedBackAsks(boards.values()), [boards])
  /** For the room's record (M45): the book as it stands. */
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const keptNotes = useMemo<KeptNotes>(() => Object.fromEntries(noteBook.current), [bookRev])
  /** The boards at a Bot that a nearer page hides now (`Board.tsx`'s report): their asks count as not held. */
  const [hidden, setHidden] = useState<ReadonlySet<string>>(() => new Set())
  const hide = useCallback((ids: ReadonlySet<string>) => setHidden(was => (was.size === ids.size && [...ids].every(id => was.has(id)) ? was : ids)), [])
  /** The asks on a board now: their panel does not come up by itself. */
  const hiddenNow = useRef(hidden)
  hiddenNow.current = hidden
  const asks = useMemo(() => new Set(views.map(v => v.needId)), [views])
  /**
   * How many of each Bot's asks a board holds in view: its sign counts the others, a hidden board's too. A board in
   * his hand that is out of his view (`awaySince`) holds none, so the Bot's sign shows while he cannot see the board.
   */
  const held = useMemo(() => {
    const m = new Map<string, number>()
    for (const v of views) if (v.need && !hidden.has(v.needId) && !(v.where === 'held' && v.awaySince !== null)) m.set(v.bot, (m.get(v.bot) ?? 0) + 1)
    return m
  }, [views, hidden])
  const probe = useCallback((): BoardProbe => ({
    boards: [...now.current.values()].map(b => { const need = needOf(b.needId); const t = need ? boardText(need) : texts.current.get(b.needId) ?? null; return { needId: b.needId, bot: b.bot, kind: b.kind, ask: need?.kind ?? '', sheet: b.sheet, ...sheetProbe(b, hostNow.current.words), ...(need?.kind === 'question' ? { sheets: questionSheets(need).length } : {}), picks: b.picks, comment: b.comment, writing: b.writing, behind: behindOf(b, crewNow.current, answered.current, hostNow.current.words), back: b.back, where: b.where, stamped: b.stamped, version: b.version, responseId: b.responseId, noId: b.noId, sentence: t?.sentence ?? '', command: t?.command ?? '', print: printsNow.current.get(b.needId) ?? null } }),
    sent: sent.current.map(s => ({ ...s })),
    releases: releases.current.map(x => ({ ...x })),
    said: [...said.current],
    holds: holds.current.map(h => ({ ...h })),
    hidden: [...hiddenNow.current],
    retried: [...cutRetried.current],
  }), [needOf])
  /** For the probe only: a held board bound to an older version of its ask, as if the ask changed after the take. */
  const stale = useCallback((id: string) => { const b = now.current.get(id); if (b && b.where !== 'bot') put({ ...b, version: `${b.version}|0` }, id) }, [put])
  /**
   * The asks a board answered or is answering now, for reading (Home's line waits for no board on these): the hook's
   * own book, so an answered ask that left the list is gone from it too, and the same id later is a new ask.
   */
  const answeredNow = useCallback((): ReadonlySet<string> => answered.current, [])
  return { views, holders, backBots, backAsks, keptNotes, asks, held, hidden, hide, take, grab, canGrab, stamp, stampBox, release, pick, corner, page, away, foot, note, probe, stale, answered: answeredNow, holdStart, holdEnd }
}
