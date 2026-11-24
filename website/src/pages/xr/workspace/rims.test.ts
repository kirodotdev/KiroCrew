import { describe, expect, it } from 'vitest'
import { FEEDS_OK, buildXrCrew, type XrBot, type XrCrew, type XrFeeds } from '../model'
import type { MemberRosterRow } from '../../../api/client'
import type { ChatSlot } from '../../../types'
import { ASKS, pressFate } from './asks'
import { newRimBook, pressCounts, pressedCuts, readRims, rimOf } from './rims'

const member = { slug: 'mira', name: 'Mira', slot_key: 'member-mira' } as MemberRosterRow
const own = (o: Partial<ChatSlot> = {}): ChatSlot => ({ key: 'member-mira', messages: 2, running: false, mode: 'member', ...o } as ChatSlot)
const worker = (o: Partial<ChatSlot> = {}): ChatSlot => ({ key: 'chat-1', messages: 2, running: false, mate_root: 'member-mira', ...o } as ChatSlot)
const fold = (slots: ChatSlot[], unread: string[] = [], feeds: XrFeeds = FEEDS_OK): XrCrew => ({ ...buildXrCrew({ members: [member], slots, approvals: [], questions: [], unread }), feeds })
const ownCut = own({ interrupted: true, turn: 4, turn_gen: 'g', last_turn_ts: '2026-11-19T10:00:00Z' })
const workerCut = worker({ interrupted: true, turn: 2, turn_gen: 'g', last_turn_ts: '2026-11-19T09:00:00Z' })
const OWN = 'member-mira@2026-11-19T10:00:00Z'
const WORKER = 'chat-1@2026-11-19T09:00:00Z'

/** The Bot as its rim draws it after these reads, with these cuts pressed. */
const rimAfter = (reads: XrCrew[], pressed: ReadonlySet<string> = new Set()): XrBot => {
  const book = newRimBook()
  for (const r of reads) readRims(book, r)
  return rimOf(reads[reads.length - 1].bots[0], book, pressed)
}
const red = (b: XrBot) => b.state === 'failed' && !b.dim
const dim = (b: XrBot) => b.state !== 'failed' && b.dim

describe('the four words of the rim', () => {
  it('a cut: the slot says interrupted, its own thread\'s or a worker\'s', () => {
    // The cut's own thread or worker, what it was doing (its first open todo task) and its time ride along (P3 slice 2).
    expect(fold([ownCut]).bots[0].cuts).toMatchObject([{ slot: 'member-mira', id: OWN, unread: false, stale: false, direct: true, doing: '' }])
    expect(fold([own(), workerCut]).bots[0].cuts).toMatchObject([{ slot: 'chat-1', id: WORKER, unread: false, stale: false, direct: false, doing: '' }])
    expect(fold([own(), worker()]).bots[0].cuts).toEqual([])
  })

  it('unread: the slot\'s key is in the gateway\'s read state', () => {
    expect(fold([own(), workerCut], ['chat-1']).bots[0].cuts[0].unread).toBe(true)
    expect(fold([own(), workerCut], ['member-mira']).bots[0].cuts[0].unread).toBe(false)
  })

  it('it began while he is in: not cut in one whole read and cut in the next, for a worker too', () => {
    const book = newRimBook()
    readRims(book, fold([own(), worker()]))
    readRims(book, fold([ownCut, workerCut]))
    expect([...book.begun].sort()).toEqual([WORKER, OWN])
    // What was cut at the opening, or when the live connection came back, did not begin while he was in.
    const opened = newRimBook()
    readRims(opened, fold([ownCut]))
    readRims(opened, fold([own()], [], { ...FEEDS_OK, live: false }))
    readRims(opened, fold([own({ interrupted: true, turn: 5, turn_gen: 'h', last_turn_ts: '2026-11-19T11:00:00Z' })]))
    expect(opened.begun.size).toBe(0)
  })

  it('he pressed that Bot: every cut of it that was read', () => {
    expect(pressedCuts(fold([ownCut, workerCut]).bots[0]).sort()).toEqual([WORKER, OWN])
  })
})

describe('the rim\'s red and its dim mark', () => {
  it('is red for a cut that he has not pressed and the gateway calls unread', () => {
    expect(red(rimAfter([fold([own(), workerCut], ['chat-1'])]))).toBe(true)
  })

  it('is red for a cut that began while he is in, and dims once he pressed that Bot', () => {
    const reads = [fold([own()]), fold([ownCut])]
    expect(red(rimAfter(reads))).toBe(true)
    expect(dim(rimAfter(reads, new Set([OWN])))).toBe(true)
  })

  it('is dim for a cut that is neither unread nor begun while he is in: his own thread\'s cut from while he was away', () => {
    expect(dim(rimAfter([fold([ownCut])]))).toBe(true)
  })

  it('is red again for a new cut after a press', () => {
    const cut5 = own({ interrupted: true, turn: 5, turn_gen: 'g', last_turn_ts: '2026-11-19T12:00:00Z' })
    expect(red(rimAfter([fold([own()]), fold([ownCut]), fold([own()]), fold([cut5])], new Set([OWN])))).toBe(true)
  })

  it('keeps needs and working first: a seen cut shows its mark beside them, an unseen one nothing', () => {
    const working = rimAfter([fold([own({ running: true }), workerCut])])
    expect(working.state).toBe('working')
    expect(working.dim).toBe(true)
    const workingRed = rimAfter([fold([own({ running: true }), workerCut], ['chat-1'])])
    expect(workingRed.state).toBe('working')
    expect(workingRed.dim).toBe(false)
  })

  it('is dim for a cut that the gateway calls old, even unread, and even one that began while he is in', () => {
    const stale = worker({ interrupted: true, interrupted_stale: true, turn: 2, turn_gen: 'g', last_turn_ts: '2026-11-19T09:00:00Z' })
    expect(dim(rimAfter([fold([own(), stale], ['chat-1'])]))).toBe(true)
    expect(dim(rimAfter([fold([own(), worker()]), fold([own(), stale])]))).toBe(true)
    // Beside it a cut that is not old and unread is red.
    expect(red(rimAfter([fold([own(), stale, ownCut], ['member-mira'])]))).toBe(true)
  })

  it('keeps needs over all: an ask beside a red cut, a seen cut, an old one and a worker that runs', () => {
    const asks = own({ pending_approval: true, running: true })
    for (const cuts of [[workerCut], [workerCut, worker({ key: 'chat-2', running: true, mate_root: 'member-mira' })]]) {
      const unseen = rimAfter([fold([asks, ...cuts], ['chat-1'])])
      expect(unseen.state).toBe('needs')
      expect(unseen.dim).toBe(false)
      const seen = rimAfter([fold([asks, ...cuts])])
      expect(seen.state).toBe('needs')
      expect(seen.dim).toBe(true)
    }
    const old = rimAfter([fold([asks, worker({ interrupted: true, interrupted_stale: true })], ['chat-1'])])
    expect(old.state).toBe('needs')
    expect(old.dim).toBe(true)
  })

  it('leaves a Bot with no cut as it is', () => {
    const b = fold([own()]).bots[0]
    expect(rimOf(b, newRimBook(), new Set())).toBe(b)
  })
})

describe('the rim\'s first mend', () => {
  it('A: a panel press counts for the rim only when the press gate sends it or it keeps the panel', () => {
    const facts = { now: 10_000, pictureAt: 0, ask: false, done: false, botPage: true, botAsks: false, pressAt: null, changedAt: null, closedAt: null }
    expect(pressCounts(pressFate(facts))).toBe(true)
    // The hold after a close: a pinch aimed at a panel that had just gone lands on this one.
    expect(pressFate({ ...facts, closedAt: 10_000 - ASKS.afterCloseMs / 2 })).toBe('hold')
    expect(pressCounts(pressFate({ ...facts, closedAt: 10_000 - ASKS.afterCloseMs / 2 }))).toBe(false)
    // A press on an ask's panel whose asks are gone only keeps it: he pressed that page.
    expect(pressFate({ ...facts, ask: true, done: true })).toBe('keep')
    expect(pressCounts('keep')).toBe(true)
  })

  it('B: the same cut keeps its key over a restart (turn_gen and turn change), and a new cut gets another', () => {
    const before = fold([own(), workerCut], ['chat-1'])
    const restarted = fold([own(), worker({ interrupted: true, turn: 0, turn_gen: 'h', last_turn_ts: '2026-11-19T09:00:00Z' })], ['chat-1'])
    expect(restarted.bots[0].cuts[0].id).toBe(before.bots[0].cuts[0].id)
    // He pressed Mira before the restart: after it, with no reload, her worker's cut is still seen.
    const pressed = new Set(pressedCuts(before.bots[0]))
    const book = newRimBook()
    readRims(book, before)
    readRims(book, fold([own(), workerCut], ['chat-1'], { ...FEEDS_OK, live: false }))
    readRims(book, restarted)
    expect(dim(rimOf(restarted.bots[0], book, pressed))).toBe(true)
    const again = fold([own(), worker({ interrupted: true, turn: 1, turn_gen: 'h', last_turn_ts: '2026-11-19T13:00:00Z' })], ['chat-1'])
    expect(again.bots[0].cuts[0].id).not.toBe(before.bots[0].cuts[0].id)
    expect(red(rimOf(again.bots[0], book, pressed))).toBe(true)
  })

  it('C: a slot that enters the list already cut did not begin while he is in; one that was there did', () => {
    const book = newRimBook()
    readRims(book, fold([own()]))
    // A worker's chat that he reopened on his Mac comes into the list cut.
    const reopened = fold([own(), workerCut])
    readRims(book, reopened)
    expect(book.begun.size).toBe(0)
    expect(dim(rimOf(reopened.bots[0], book, new Set()))).toBe(true)
    // A slot that was in the last read and not cut, then cut: begun.
    const b2 = newRimBook()
    readRims(b2, fold([own(), worker()]))
    readRims(b2, fold([own(), workerCut]))
    expect([...b2.begun]).toEqual([WORKER])
    // A slot that was cut with another key, then cut anew without a read in between: begun.
    const b3 = newRimBook()
    readRims(b3, fold([own(), workerCut]))
    readRims(b3, fold([own(), worker({ interrupted: true, turn: 3, turn_gen: 'g', last_turn_ts: '2026-11-19T14:00:00Z' })]))
    expect([...b3.begun]).toEqual(['chat-1@2026-11-19T14:00:00Z'])
  })
})
