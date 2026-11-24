import { describe, expect, it } from 'vitest'
import type { XrNeed } from '../model'
import { BOARD, STAY_MOVED, awayTick, cancelMoved, upTakesBoard, boardAskOf, boardFor, markBack, boardText, boardsNow, floorEnds, grabAsk, handBack, handSpeed, handedBack, heldFull, oldestOverHeld, pickUp, previewMoved, askBoardShiftM, putBackAsIs, releaseKind, sheetTopDeg, sessionMoved, stampBoard, takeBoard, throwBoard, waitingBehind, waitsBehindAsk, type Board } from './board'

const need = (id: string, over: Partial<XrNeed> = {}): XrNeed => ({
  id, kind: 'approval', card: true, bot: 'mira', who: 'Mira', topic: 'release', slot: 's1', action: 'Run the release script', tool: 'shell',
  command: 'npm run release', extra: [], version: `${id}|m1|npm run release`, purpose: '', origin: 'native', requestId: `r-${id}`, requestMid: 'm1', questions: [], ts: 1, ...over,
})
let ids = 0
const mint = () => `resp-${(ids += 1)}`
const held = (n: XrNeed, at = 0): Board => takeBoard(boardsNow(new Map(), [{ slug: 'mira', needs: [n] }]).boards.get(n.id) as Board, n, at)

it('marks a board handed back again after a reload, once, and only a new ask board at its Bot', () => {
  const need = { id: 'a1', bot: 'juno', version: 'v' } as XrNeed
  const fresh = new Map([['a1', boardFor(need)]])
  expect(markBack(fresh, new Map(), new Set(['a1'])).map(b => [b.needId, b.back])).toEqual([['a1', true]])
  expect(markBack(fresh, fresh, new Set(['a1']))).toEqual([])
  expect(markBack(fresh, new Map(), new Set())).toEqual([])
  expect(markBack(new Map([['a1', { ...boardFor(need), where: 'held' as const }]]), new Map(), new Set(['a1']))).toEqual([])
})

describe('one board at a time for a Bot (D24)', () => {
  // Mira's own ask, and a worker of hers that asks after it (the worker's ask folds into Mira).
  const own = need('own', { slot: 'member-mira', ts: 1 })
  const worker = need('w', { slot: 'chat-9', origin: 'native', ts: 2 })
  const q = need('q', { kind: 'question', slot: 'chat-8', ts: 3, requestId: '' })
  it('counts what waits behind the board: every other ask of the Bot, and none a board answered', () => {
    const bot = { slug: 'mira', needs: [own, worker, q] }
    expect(waitingBehind(bot, 'own', new Set())).toBe(2)
    expect(waitingBehind(bot, 'own', new Set(['w']))).toBe(1)
    expect(waitingBehind({ slug: 'mira', needs: [own] }, 'own', new Set())).toBe(0)
  })
  it('brings the next approval up as a board when the first ends', () => {
    const first = boardsNow(new Map(), [{ slug: 'mira', needs: [own, worker] }]).boards
    expect([...first.keys()]).toEqual(['own'])
    // Stamped in his hand, it stays the Bot's one board until he throws it.
    const stamped = new Map([['own', { ...takeBoard(first.get('own') as Board, own, 0), stamped: true }]])
    expect([...boardsNow(stamped, [{ slug: 'mira', needs: [worker] }]).boards.keys()]).toEqual(['own'])
    // Thrown (or answered elsewhere): the worker's ask comes up as the next board.
    expect([...boardsNow(new Map(), [{ slug: 'mira', needs: [worker] }]).boards.keys()]).toEqual(['w'])
  })
  it('lets a Home line grab the ask behind the board: the board at the Bot steps back, that ask\'s comes into his hand', () => {
    const first = boardsNow(new Map(), [{ slug: 'mira', needs: [own, worker] }]).boards
    const grabbed = grabAsk(first, worker, 5) as Map<string, Board>
    expect([...grabbed]).toEqual([['w', expect.objectContaining({ needId: 'w', where: 'held', calmFrom: 5 })]])
    // It stands: the next read keeps the grabbed board, and the first ask waits behind it.
    expect([...boardsNow(grabbed, [{ slug: 'mira', needs: [own, worker] }]).boards.keys()]).toEqual(['w'])
    // Its own board at the Bot is simply taken.
    expect(grabAsk(first, own, 5)?.get('own')?.where).toBe('held')
  })
  it('grabs nothing behind a board in his hand, on the floor or stamped, nor a question', () => {
    const first = boardsNow(new Map(), [{ slug: 'mira', needs: [own, worker] }]).boards
    const inHand = new Map([['own', takeBoard(first.get('own') as Board, own, 0)]])
    expect(grabAsk(inHand, worker, 5)).toBeNull()
    expect(grabAsk(new Map([['own', { ...(first.get('own') as Board), where: 'floor' as const }]]), worker, 5)).toBeNull()
    expect(grabAsk(new Map([['own', { ...(first.get('own') as Board), stamped: true }]]), worker, 5)).toBeNull()
    expect(grabAsk(first, q, 5)).toBeNull()
    expect(grabAsk(new Map(), worker, 5)).toBeNull()
  })
})

describe('a board', () => {
  it('is held up for a Bot\'s oldest approval only, one board per Bot, and never for a question', () => {
    const a = need('a')
    const b = need('b', { ts: 2 })
    const q = need('q', { kind: 'question' })
    expect(boardAskOf({ needs: [q, a, b] })?.id).toBe('a')
    const { boards } = boardsNow(new Map(), [{ slug: 'mira', needs: [q, a, b] }])
    expect([...boards.keys()]).toEqual(['a'])
    expect(boardAskOf({ needs: [q] })).toBeNull()
    // A worker's ask that came first is the older one, wherever its thread stands in the list.
    expect(boardAskOf({ needs: [b, need('w', { ts: 0, origin: 'coordinator' })] })?.id).toBe('w')
  })
  it('keeps a held board\'s ask when its Bot loses its seat, with no line; a board still at that Bot goes quietly', () => {
    const a = need('a')
    const crewNow = [{ slug: 'mira', needs: [a] }]
    const heldOne = held(a, 0)
    const kept = boardsNow(new Map([['a', heldOne]]), crewNow, new Set(), new Set())
    expect(kept.boards.get('a')?.where).toBe('held')
    expect(kept.gone).toEqual([])
    const atBot = boardsNow(new Map(), crewNow).boards.get('a') as Board
    const quiet = boardsNow(new Map([['a', atBot]]), crewNow, new Set(), new Set())
    expect(quiet.boards.size).toBe(0)
    expect(quiet.gone).toEqual([])
  })
  it('holds up no second board for an ask a board answered while it is still listed: the next ask comes up', () => {
    const { boards } = boardsNow(new Map(), [{ slug: 'mira', needs: [need('a'), need('b', { ts: 2 })] }], new Set(['a']))
    expect([...boards.keys()]).toEqual(['b'])
  })
  it('shows one sentence and the whole command under it', () => {
    expect(boardText(need('a'))).toEqual({ sentence: 'Mira: Run the release script', command: 'npm run release' })
    expect(boardText(need('a', { command: '', action: '' }))).toMatchObject({ sentence: 'Mira: shell', command: 'shell' })
  })
  it('takes no stamp in its first second in his hand', () => {
    const b = held(need('a'), 1000)
    expect(stampBoard(b, need('a'), 1500, mint)).toEqual({ do: 'wait' })
    expect(stampBoard(b, need('a'), 2100, mint).do).toBe('answer')
  })
  it('refuses a stamp when the ask changed since he took it: the new text, and a second with no stamp', () => {
    const b = held(need('a'), 0)
    const changed = need('a', { version: 'a|m2|npm run release --force', command: 'npm run release --force' })
    const fate = stampBoard(b, changed, 5000, mint)
    expect(fate.do).toBe('changed')
    if (fate.do !== 'changed') return
    expect(fate.board.version).toBe(changed.version)
    expect(stampBoard(fate.board, changed, 5500, mint)).toEqual({ do: 'wait' })
    expect(stampBoard(fate.board, changed, 6100, mint).do).toBe('answer')
  })
  it('answers only the ask it shows: two approvals of one Bot, the stamp is the oldest\'s', () => {
    const a = need('a')
    const b = need('b', { ts: 2 })
    const board = held(a, 0)
    expect(stampBoard(board, b, 5000, mint)).toEqual({ do: 'gone' })
    expect(stampBoard(board, a, 5000, mint)).toMatchObject({ do: 'answer', board: { needId: 'a', stamped: true } })
  })
  it('ends when its ask was answered elsewhere: the stamp finds nothing to answer, and the board goes with a line', () => {
    const board = held(need('a'), 0)
    expect(stampBoard(board, null, 5000, mint)).toEqual({ do: 'gone' })
    const { boards, gone } = boardsNow(new Map([['a', board]]), [{ slug: 'mira', needs: [] }])
    expect(boards.size).toBe(0)
    expect(gone.map(g => g.needId)).toEqual(['a'])
  })
  it('keeps one id for its decision, so a retry of the stamp is the same answer', () => {
    const first = stampBoard(held(need('a'), 0), need('a'), 5000, mint)
    if (first.do !== 'answer') throw new Error('no answer')
    // The write did not go: it takes a stamp again, and the retry sends the same id.
    const again = stampBoard({ ...first.board, stamped: false }, need('a'), 9000, mint)
    expect(again).toMatchObject({ do: 'answer', board: { responseId: first.board.responseId } })
    expect(stampBoard(first.board, need('a'), 9000, mint)).toEqual({ do: 'nothing' })
  })
  it('answers at the first stamp however long its command is: the board in his hand shows it whole', () => {
    const long = need('a', { command: 'x'.repeat(4000) })
    expect(stampBoard(held(long, 0), long, 5000, mint).do).toBe('answer')
  })
  it('is a throw only when moved far and still fast at the release; less is a hand-back; a click does nothing', () => {
    // In a session: far AND fast; a still pinch stays; a release with the ray on its own Bot is a hand-back.
    expect(releaseKind({ handM: 0.005 })).toBe('stay')
    expect(releaseKind({ handM: 0.02, handMps: 2 })).toBe('back')
    expect(releaseKind({ handM: BOARD.throwM, handMps: 0.3 })).toBe('back')
    expect(releaseKind({ handM: BOARD.throwM, handMps: BOARD.throwMps })).toBe('throw')
    // The pinch's own reading at the opening (its speed over 150 ms) is the press's when it is the same hand's: at
    // 0.8 m/s and 9 cm it is a throw; another hand's reading is not taken.
    const own = { hand: 'right' as const, handM: 0.02, handMps: 0.1 }
    expect(releaseKind(sessionMoved(own, { hand: 'right', travelM: BOARD.throwM, mps: 0.8 }))).toBe('throw')
    expect(releaseKind(sessionMoved(own, { hand: 'right', travelM: BOARD.throwM, mps: 0.79 }))).toBe('back')
    expect(sessionMoved(own, { hand: 'left', travelM: 0.3, mps: 2 })).toEqual({ handM: 0.02, handMps: 0.1 })
    expect(sessionMoved(own, null)).toEqual({ handM: 0.02, handMps: 0.1 })
    expect(releaseKind({ handM: 0.3, handMps: 2, onBot: true })).toBe('back')
    // In the preview: far, fast over the last of the movement, and the last move just before the release.
    expect(releaseKind({ px: 0 })).toBe('stay')
    expect(releaseKind({ px: 300, pxPerMs: 1.5, lastMs: 20 })).toBe('throw')
    expect(releaseKind({ px: 300, pxPerMs: 1.5, lastMs: 120 })).toBe('back')
    expect(releaseKind({ px: 300, pxPerMs: 0.2, lastMs: 10 })).toBe('back')
    expect(releaseKind({ px: 40, pxPerMs: 2, lastMs: 10 })).toBe('back')
    expect(releaseKind({ px: 300, pxPerMs: 1.5, lastMs: 20, onBot: true })).toBe('back')
  })
  it('reads the speed over the last of the movement: a drag that slowed to place the board is no throw', () => {
    const down = { x: 0, y: 0, t: 0 }
    // A flick: fast to the end, released right after.
    const flick = [{ x: 100, y: 0, t: 40 }, { x: 200, y: 0, t: 80 }, { x: 300, y: 0, t: 120 }]
    expect(releaseKind(previewMoved(down, flick, { x: 300, y: 0, t: 135 }))).toBe('throw')
    // Fast at first, then slow for its last 100 ms before the release: it was placed.
    const placed = [{ x: 250, y: 0, t: 80 }, { x: 270, y: 0, t: 130 }, { x: 280, y: 0, t: 180 }]
    expect(releaseKind(previewMoved(down, placed, { x: 280, y: 0, t: 200 }))).toBe('back')
    // Fast to the end, then held still before the release.
    expect(releaseKind(previewMoved(down, flick, { x: 300, y: 0, t: 300 }))).toBe('back')
    expect(handSpeed([{ at: [0, 0, 0], t: 0 }, { at: [0.05, 0, 0], t: 50 }, { at: [0.1, 0, 0], t: 100 }])).toBeCloseTo(1, 5)
  })
  it('thrown with no stamp lies on the floor; picked up in its time no answer goes; at its end the no goes once, with its own id', () => {
    const board = held(need('a'), 0)
    const thrown = throwBoard(board, 10_000)
    expect(thrown.particles).toBe(false)
    const floor = thrown.board as Board
    expect(floor.where).toBe('floor')
    expect(floorEnds(floor, need('a'), 10_000 + BOARD.floorMs - 1, mint)).toBeNull()
    const back = pickUp(floor, 12_000)
    expect(back).toMatchObject({ where: 'held', responseId: '', noId: '' })
    const due = floorEnds(floor, need('a'), 10_000 + BOARD.floorMs, mint)
    expect(due).toMatchObject({ do: 'no', board: { noId: expect.stringMatching(/^resp-/) } })
  })
  it('never sends the no with the yes\'s id: a stamp whose reply was lost, then thrown, mints a new id for the no', () => {
    const first = stampBoard(held(need('a'), 0), need('a'), 5000, mint)
    if (first.do !== 'answer') throw new Error('no answer')
    // The reply was lost: the board takes a stamp again, and he throws it instead.
    const floor = throwBoard({ ...first.board, stamped: false }, 6000).board as Board
    const due = floorEnds(floor, need('a'), 6000 + BOARD.floorMs, mint)
    if (due?.do !== 'no') throw new Error('no no')
    expect(due.board.noId).not.toBe('')
    expect(due.board.noId).not.toBe(first.board.responseId)
  })
  it('sends no no for an ask that changed while its board lay on the floor: the board goes back to its Bot', () => {
    const floor = throwBoard(held(need('a'), 0), 10_000).board as Board
    const changed = need('a', { version: 'a|m2|npm run release --force' })
    expect(floorEnds(floor, changed, 10_000 + BOARD.floorMs, mint)).toMatchObject({ do: 'back', board: { where: 'bot', noId: '' } })
    expect(floorEnds(floor, null, 10_000 + BOARD.floorMs, mint)).toBeNull()
  })
  it('thrown after the stamp falls apart: the yes went at the stamp', () => {
    const first = stampBoard(held(need('a'), 0), need('a'), 5000, mint)
    if (first.do !== 'answer') throw new Error('no answer')
    expect(throwBoard(first.board, 6000)).toEqual({ board: null, particles: true })
    // It stays in his hand after its ask is answered, until he throws it.
    expect(boardsNow(new Map([['a', first.board]]), [{ slug: 'mira', needs: [] }]).boards.get('a')?.stamped).toBe(true)
  })
  it('goes back to its Bot, with no answer, when handed back or out of his view for a minute', () => {
    const board = held(need('a'), 0)
    expect(handBack(board).where).toBe('bot')
    const away = awayTick(board, false, 1000)
    expect(awayTick(away, false, 1000 + BOARD.awayMs - 1).where).toBe('held')
    expect(awayTick(away, false, 1000 + BOARD.awayMs).where).toBe('bot')
    expect(awayTick(away, true, 2000).awaySince).toBeNull()
  })
  it('marks only his own hand-back, and lets the Bot\'s questions past that board alone (D27)', () => {
    const q = { ...need('q'), kind: 'question' as const }
    const bot = { slug: 'mira', needs: [need('a'), need('w'), q] }
    const mine = handBack(held(need('a'), 0), true)
    expect(mine).toMatchObject({ where: 'bot', back: true })
    // Out of his view for a minute, or its ask changed on the floor: it goes back by itself, and is not marked.
    const away = awayTick(awayTick(held(need('a'), 0), false, 1000), false, 1000 + BOARD.awayMs)
    expect(away).toMatchObject({ where: 'bot', back: false })
    const floor = throwBoard(held(need('a'), 0), 0).board as Board
    expect(floorEnds(floor, { ...need('a'), version: 'v2' }, BOARD.floorMs, () => 'n')?.board.back).toBe(false)
    // Taken again, the mark is gone.
    expect(takeBoard(mine, need('a'), 5).back).toBe(false)
    // Its questions are not counted on its tag; behind an unmarked board they are.
    expect(waitingBehind(bot, 'a', new Set(), true)).toBe(1)
    expect(waitingBehind(bot, 'a', new Set(), false)).toBe(2)
    // A Bot counts as handed back only while that board stands at it.
    expect([...handedBack([mine])]).toEqual(['mira'])
    expect([...handedBack([held(need('a'), 0)])]).toEqual([])
    expect([...handedBack([away])]).toEqual([])
  })
})

describe('at most two boards in his hand (D75)', () => {
  const three = () => new Map<string, Board>([['a', held(need('a'), 10)], ['b', held(need('b'), 20)], ['c', held(need('c'), 30)]])
  it('names no board while he holds two', () => {
    expect(oldestOverHeld(new Map([['a', held(need('a'), 10)], ['b', held(need('b'), 20)]]))).toBeNull()
  })
  it('names the board he took first when he holds a third, and says whether it is stamped', () => {
    expect(oldestOverHeld(three())).toEqual({ id: 'a', stamped: false })
    expect(oldestOverHeld(three().set('a', { ...held(need('a'), 10), stamped: true }))).toEqual({ id: 'a', stamped: true })
  })
  it('does not count a board at its Bot, on the floor, or the sheet he speaks on', () => {
    expect(oldestOverHeld(three().set('a', handBack(held(need('a'), 10))))).toBeNull()
    expect(oldestOverHeld(three().set('b', { ...held(need('b'), 20), where: 'floor' }))).toBeNull()
    expect(oldestOverHeld(three().set('c', { ...held(need('c'), 30), kind: 'speak' }))).toBeNull()
  })
})

describe('a board that a third take puts back (D79 2)', () => {
  it('goes back to its Bot as it is: his prints, marks and words kept, his writing ended', () => {
    const b = { ...held(need('a'), 10), writing: true, comment: 'half a note', picks: { 0: ['Oregon'] }, marks: { '0|Oregon': { u: 0.5, v: 0.5, r: 0 } } } as unknown as Board
    const back = putBackAsIs(b)
    expect(back).toMatchObject({ where: 'bot', back: false, put: true, writing: false, comment: 'half a note', picks: b.picks, marks: b.marks })
  })
  it('stands at its Bot with his note while its ask lives: a question or the Bot\'s words do not take its place (M36)', () => {
    const a = need('a')
    const put = putBackAsIs({ ...held(a, 10), comment: 'ship it after lunch' })
    const q = need('q', { kind: 'question', ts: 2 } as Partial<typeof a>)
    const words = new Map([['mira', { id: 'w:w1', version: 'v1', slot: 'w1', raise: true, state: 'ok' as const, sentence: 'Done.', sheets: [] }]])
    const { boards } = boardsNow(new Map([['a', put]]), [{ slug: 'mira', needs: [a, q] }], new Set(), undefined, words)
    expect([...boards.keys()]).toEqual(['a'])
    expect(boards.get('a')).toMatchObject({ where: 'bot', comment: 'ship it after lunch', put: true })
    // Taken again, his note is on it, and it is put by no more.
    expect(takeBoard(boards.get('a') as Board, a, 20)).toMatchObject({ where: 'held', comment: 'ship it after lunch', put: false })
  })
  it('an ask outranks a sheet put by with his note: its board shows, and the sheet waits behind with the note (F1)', () => {
    const sheet = putBackAsIs({ ...held(need('x'), 10), needId: 'w:w1', kind: 'words', comment: 'see me' } as Board)
    const a = need('a')
    const { boards } = boardsNow(new Map([['w:w1', sheet]]), [{ slug: 'mira', needs: [a] }])
    expect([...boards.keys()].sort()).toEqual(['a', 'w:w1'])
    expect(boards.get('w:w1')).toMatchObject({ comment: 'see me', put: true })
    expect(waitsBehindAsk(boards.get('w:w1') as Board, boards.values())).toBe(true)
    expect(waitsBehindAsk(boards.get('a') as Board, boards.values())).toBe(false)
  })
  it('a board at its Bot with his note stays when its ask ends elsewhere, marked so; one with no note goes quietly (F2)', () => {
    const noted = putBackAsIs({ ...held(need('a'), 10), comment: 'ship it after lunch' })
    const kept = boardsNow(new Map([['a', noted]]), [{ slug: 'mira', needs: [] }])
    expect(kept.boards.get('a')).toMatchObject({ where: 'bot', comment: 'ship it after lunch', elsewhere: true })
    expect(kept.gone).toHaveLength(0)
    const bare = putBackAsIs(held(need('a'), 10))
    const dropped = boardsNow(new Map([['a', bare]]), [{ slug: 'mira', needs: [] }])
    expect(dropped.boards.has('a')).toBe(false)
  })
  it('a sheet put by with his note stays at its Bot when its words are raised no more (M36)', () => {
    const sheet = { ...held(need('a'), 10), needId: 'w:w1', kind: 'words', comment: 'see me' } as Board
    const { boards } = boardsNow(new Map([['w:w1', putBackAsIs(sheet)]]), [{ slug: 'mira', needs: [] }])
    expect(boards.get('w:w1')).toMatchObject({ where: 'bot', comment: 'see me' })
  })
  it('is never the one he writes on: the oldest board he is not writing on goes', () => {
    const boards = new Map<string, Board>([['a', { ...held(need('a'), 10), writing: true }], ['b', held(need('b'), 20)], ['c', held(need('c'), 30)]])
    expect(oldestOverHeld(boards)).toEqual({ id: 'b', stamped: false })
  })
  it('with both in his hand written on, a take does nothing', () => {
    const two = [{ ...held(need('a'), 10), writing: true }, { ...held(need('b'), 20), writing: true }]
    expect(heldFull(two)).toBe(true)
    expect(heldFull([two[0], held(need('b'), 20)])).toBe(false)
  })
})

describe('where a held sheet\'s top hangs (M42, M43)', () => {
  const eye = { x: 0, y: 1.6, z: 0 }
  // A strip 1.75 m ahead whose lower edge is 16.17 degrees over level (`xr-15`), and one at 17.56 (`xr-need`).
  const strip = (lowDeg: number) => ({ center: { x: 0, y: 1.6 + Math.tan((lowDeg * Math.PI) / 180) * 1.75 + 0.03, z: -1.75 }, heightM: 0.06 })
  it('hangs a degree under the strip\'s lower edge, wherever the strip stands', () => {
    expect(sheetTopDeg(eye, 19, 20.3, strip(16.17))).toBeCloseTo(15.17, 1)
    expect(sheetTopDeg(eye, 19, 20.3, strip(17.56))).toBeCloseTo(16.56, 1)
  })
  it('is never over the lens\'s top at the take less a degree: his head down at a low Bot', () => {
    expect(sheetTopDeg(eye, -9.6, 20.35, strip(17.56))).toBeCloseTo(9.75, 1)
  })
  it('has its own line with no strip drawn', () => {
    expect(sheetTopDeg(eye, 0, 20.3, null)).toBe(16.5)
  })
})

describe('an ask\'s board whole in the lens at the take (D92 2)', () => {
  const deg = (m: number) => (Math.atan(m / 0.75) * 180) / Math.PI
  const edges = (h: number, lens: { pitchDeg: number; halfDeg: number; lineDeg: number }) => { const s = askBoardShiftM(h, 0.75, 0.04, lens); return { top: deg(h / 2 - 0.04 + s), bottom: deg(-h / 2 - 0.04 + s) } }
  it('with his head 9 degrees up a board of 0.33 m has both edges in the lens, under Home\'s line', () => {
    const e = edges(0.33, { pitchDeg: 9, halfDeg: 20.3, lineDeg: 16.5 })
    expect(e.top).toBeLessThanOrEqual(16.51)
    expect(e.bottom).toBeGreaterThanOrEqual(9 - 20.3 + 0.99)
  })
  it('with his head 9 degrees down its top is a degree inside the lens\'s top', () => {
    expect(edges(0.45, { pitchDeg: -9, halfDeg: 20.3, lineDeg: 16.5 }).top).toBeCloseTo(10.3, 1)
  })
  it('a board taller than the room keeps its top: head up, the tallest board hangs from Home\'s line', () => {
    expect(edges(0.45, { pitchDeg: 9, halfDeg: 20.3, lineDeg: 16.5 }).top).toBeCloseTo(16.5, 1)
  })
  it('does not move a board that is whole already', () => {
    expect(askBoardShiftM(0.3, 0.75, 0.04, { pitchDeg: 0, halfDeg: 20.3, lineDeg: 16.5 })).toBe(0)
  })
})

describe('an up takes a board only after its own down (DASH-16 D)', () => {
  const downs = (...d: [number, 'bot' | 'held' | 'floor'][]) => new Map(d)
  it.each([
    { name: 'must not take: the down was on a tray (the board saw no down)', d: downs(), id: 7, where: 'bot' as const, takes: false },
    { name: 'must not take: the down was on the room', d: downs(), id: 7, where: 'bot' as const, takes: false },
    { name: 'must not take: the down was on another Bot\'s board (its own downs, not this one\'s)', d: downs(), id: 7, where: 'bot' as const, takes: false },
    { name: 'must not take: another pointer\'s down on it', d: downs([8, 'bot']), id: 7, where: 'bot' as const, takes: false },
    { name: 'must not take: a down made before it came to its Bot', d: downs([7, 'held']), id: 7, where: 'bot' as const, takes: false },
    { name: 'must take: a plain pinch on it at its Bot', d: downs([7, 'bot']), id: 7, where: 'bot' as const, takes: true },
    { name: 'must take: a down on it that left and came back (a leave keeps the down)', d: downs([7, 'bot']), id: 7, where: 'bot' as const, takes: true },
    { name: 'must take: a pick from the floor after a down there', d: downs([7, 'floor']), id: 7, where: 'floor' as const, takes: true },
    { name: 'a held board is not taken by an up', d: downs([7, 'held']), id: 7, where: 'held' as const, takes: false },
  ])('$name', ({ d, id, where, takes }) => {
    expect(upTakesBoard(d, id, where)).toBe(takes)
  })
  it('two pointers, one down on each of two boards: each up takes only its own', () => {
    const a = downs([7, 'bot']), b = downs([8, 'bot'])
    expect([upTakesBoard(a, 7, 'bot'), upTakesBoard(a, 8, 'bot'), upTakesBoard(b, 8, 'bot'), upTakesBoard(b, 7, 'bot')]).toEqual([true, false, true, false])
  })
})

describe('a held board\'s press that ends by a cancel (D192, 15:50)', () => {
  const fast = { handM: 0.2, handMps: 1.5 }
  const slow = { handM: 0.05, handMps: 0.2 }
  it('must not act: a cancel that is not his let-go is a stay, however fast the drag', () => {
    expect(releaseKind(cancelMoved(false, fast))).toBe('stay')
    expect(releaseKind(cancelMoved(false, slow))).toBe('stay')
    expect(cancelMoved(false, fast)).toBe(STAY_MOVED)
  })
  it('must not lose: his own let-go that comes as a cancel is read as his release is', () => {
    expect(releaseKind(cancelMoved(true, fast))).toBe('throw')
    expect(releaseKind(cancelMoved(true, slow))).toBe('back')
    expect(releaseKind(fast)).toBe('throw')
  })
})
