import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { xrFixture } from '../fixture'
import { buildXrCrew, type XrBot, type XrNeed, type XrThing } from '../model'
import { BOARD, boardAskOf, boardsNow } from './board'
import { sinceLinePitch } from './since'
import { HOME, STRIP_TOP_MOST_DEG, behindTakenBoard, firstPageTop, boardComes, doneLines, doneNow, homeDoneRows, homeForm, homeLayout, homeLines, homeStats, keepDone, lineDoes, lineOpens, threeWords } from './home'
import { PAGES, M_PER_PX } from './layout'
import { useBoards } from './useBoards'
import { pagesHiddenAtStart, readRoom, readWalls, writeRoom, type KeptPage } from './room'

afterEach(() => { vi.restoreAllMocks() })

/** Noon of a local day, so that "today" is the same in every time zone. */
const NOON = new Date(2026, 10, 15, 12, 0, 0).getTime()
const at = (hoursAgo: number) => Math.floor((NOON - hoursAgo * 3_600_000) / 1000)
const thing = (state: XrThing['state'], hoursAgo: number, direct = false): XrThing => ({ slot: `s-${state}-${hoursAgo}`, direct, topic: '', line: '', state, ts: at(hoursAgo) })
const bot = (state: XrBot['state'], things: XrThing[], lastHoursAgo: number, slug = 'b'): Pick<XrBot, 'slug' | 'state' | 'things' | 'lastTs'> => ({ slug, state, things, lastTs: at(lastHoursAgo) })

describe('Home\'s "done today" (D35)', () => {
  const rest = (slot: string, hoursAgo: number, unread = false) => ({ slot, direct: false, topic: `Work ${slot}`, line: '', ts: at(hoursAgo), unread, made: [], left: [] })

  it('lists what he filed today, newest first, five at most; not what waits to be read, nor yesterday\'s', () => {
    const bots = [
      { slug: 'juno', label: 'Juno', rest: [rest('a', 1), rest('b', 2), rest('c', 3, true), rest('old', 20)] },
      { slug: 'mira', label: 'Mira', rest: [rest('d', 0.5), rest('e', 4), rest('f', 5), rest('g', 6)] },
    ]
    const lines = doneLines(bots, NOON)
    expect(lines.map(l => l.slot)).toEqual(['d', 'a', 'b', 'e', 'f'])
    expect(lines[0]).toMatchObject({ id: 'd:d', bot: 'mira', who: 'Mira', words: 'Work d' })
    // The answer's first line comes whole (Home cuts it at the width it has), before the work's title.
    const said = doneLines([{ slug: 'juno', label: 'Juno', rest: [{ ...rest('x', 1), line: 'Here is a short draft for the beta notes.' }] }], NOON)
    expect(said[0].words).toBe('Here is a short draft for the beta notes.')
    expect(HOME.doneLines).toBe(5)
  })

  it('makes room under the lines of what waits for its head and its lines, in the large form only', () => {
    const plain = homeLayout('large', 2, false)
    const done = homeLayout('large', 2, false, false, { head: true, lines: 3 })
    expect(done.doneHead).not.toBeNull()
    expect(done.done).toHaveLength(3)
    expect(done.heightM - plain.heightM).toBeCloseTo(4 * HOME.large.lineM)
    // Every row is under the one before it, and under the asks' lines.
    const rows = [...done.lines, done.doneHead!, ...done.done].map(r => r.cy)
    expect(rows.every((y, i) => i === 0 || y < rows[i - 1])).toBe(true)
    expect(homeLayout('compact', 0, false, false, { head: true, lines: 3 }).done).toEqual([])
  })
})

describe('the numbers of the Home surface', () => {
  it('counts the Bots that work and the Bots that failed by their state, and every ask that waits', () => {
    const bots = [bot('working', [thing('working', 0, true)], 0), bot('working', [thing('working', 1, true)], 1), bot('failed', [thing('failed', 2, true)], 2), bot('needs', [thing('needs', 0)], 0), bot('idle', [], 30)]
    expect(homeStats(bots, [{}, {}, {}], NOON)).toEqual({ working: 2, waiting: 3, failed: 1, done: 0 })
  })

  it('counts as done today a thing at rest that was last active today, and not one of yesterday', () => {
    const bots = [bot('working', [thing('working', 0, true), thing('idle', 1), thing('idle', 3), thing('idle', 20), thing('failed', 1), thing('needs', 1)], 0)]
    expect(homeStats(bots, [], NOON).done).toBe(2)
  })

  it('counts a Bot own thread at rest once, when it was the last of the Bot to be active today', () => {
    // The thread answered an hour ago, after the Bot's handed-out work: one more piece of work came to rest.
    expect(homeStats([bot('idle', [thing('idle', 3)], 1)], [], NOON).done).toBe(2)
    // Nothing of the Bot is newer than its handed-out work: the thread is not counted.
    expect(homeStats([bot('idle', [thing('idle', 3)], 3)], [], NOON).done).toBe(1)
    // Yesterday's answer, and a thread that was cut, are not done today.
    expect(homeStats([bot('idle', [], 20), bot('failed', [], 1)], [], NOON).done).toBe(0)
  })

  it('does not take a finish away when the Bot starts again, and does not count it twice', () => {
    const rested = [bot('idle', [thing('idle', 3)], 1, 'otto')]
    let book = keepDone(null, doneNow(rested, NOON), NOON)
    expect([...book.ids].sort()).toEqual(['own:otto', 's-idle-3'])
    expect(homeStats(rested, [], NOON, book.ids).done).toBe(2)
    // Otto starts again: its own thread works, and its worker too. Nothing counts as done now, and the book keeps both.
    const again = [bot('working', [{ ...thing('working', 0, true) }, { ...thing('working', 0), slot: 's-idle-3' }], 0, 'otto')]
    expect(doneNow(again, NOON)).toEqual([])
    book = keepDone(book, doneNow(again, NOON), NOON)
    expect(homeStats(again, [], NOON, book.ids).done).toBe(2)
    // It rests again: the same two pieces, counted once.
    const same = keepDone(book, doneNow(rested, NOON), NOON)
    expect(same).toBe(book)
    // Another piece comes to rest: one more.
    const more = [bot('idle', [thing('idle', 3), thing('idle', 2)], 1, 'otto')]
    expect(keepDone(book, doneNow(more, NOON), NOON).ids.size).toBe(3)
  })

  it('starts the count anew when the day turns', () => {
    const book = keepDone(null, ['a', 'b'], NOON)
    const next = keepDone(book, ['c'], NOON + 24 * 3_600_000)
    expect([...next.ids]).toEqual(['c'])
    expect(next.day).toBeGreaterThan(book.day)
  })

  it('agrees with the model of the sample crew', () => {
    const crew = buildXrCrew(xrFixture(false))
    const stats = homeStats(crew.bots, crew.needs, NOON)
    expect(stats.working).toBe(crew.bots.filter(b => b.state === 'working').length)
    expect(stats.failed).toBe(crew.bots.filter(b => b.state === 'failed').length)
    expect(stats.waiting).toBe(crew.needs.length)
    expect(stats.working).toBeGreaterThan(0)
    expect(stats.waiting).toBeGreaterThan(0)
    expect(stats.failed).toBeGreaterThan(0)
  })
})

describe('the lines of what waits', () => {
  it('says who and three words', () => {
    expect(threeWords('Run shard 3 again without retries')).toBe('Run shard 3…')
    expect(threeWords('Pick a region.')).toBe('Pick a region')
    expect(threeWords('')).toBe('')
    // A line with no spaces is cut by its characters.
    expect(threeWords('我需要在没有重试的情况下再次运行第三个分片')).toBe('我需要在没有重试的情况下…')
  })

  it('shows the oldest three and counts the rest', () => {
    const needs = [1, 2, 3, 4, 5].map(i => ({ id: `n${i}`, bot: `b${i}`, who: `Bot ${i}`, action: `Do thing number ${i}`, topic: '', kind: 'approval' as const, card: true }))
    const { lines, more } = homeLines(needs)
    expect(lines.map(l => l.id)).toEqual(['n1', 'n2', 'n3'])
    expect(lines[0]).toEqual({ id: 'n1', bot: 'b1', who: 'Bot 1', words: 'Do thing number…', opens: 'bot' })
    expect(more).toBe(2)
    expect(homeLines([{ id: 'a', bot: '', who: 'Mira', action: '', topic: 'Weekly growth report', kind: 'question', card: true }]).lines[0].words).toBe('Weekly growth report')
  })

  it('takes the board of an approval out of Home, and never opens a second way to answer it', () => {
    // Its Bot holds the board up: the press takes that board.
    expect(lineDoes({ opens: 'bot' }, 'bot')).toBe('take')
    expect(lineDoes({ opens: 'view' }, 'bot')).toBe('take')
    // An approval behind its Bot's board at the Bot grabs its own board (D24); one a board answered does nothing.
    expect(lineDoes({ opens: 'bot' }, undefined, false, false, true)).toBe('take')
    expect(lineDoes({ opens: 'bot' }, undefined, false, true, true)).toBe('none')
    // The board is in his hand, or on the floor: the press does nothing more, and no page opens beside the board.
    expect(lineDoes({ opens: 'bot' }, 'held')).toBe('none')
    expect(lineDoes({ opens: 'bot' }, 'floor')).toBe('none')
    // No board shows this ask: it opens as its line says, or nowhere.
    expect(lineDoes({ opens: 'bot' }, undefined)).toBe('bot')
    expect(lineDoes({ opens: 'view' }, undefined)).toBe('view')
    expect(lineDoes({ opens: null }, undefined)).toBe('none')
  })

  it('does nothing for an ask behind its Bot\'s board in his hand, on the floor or stamped, and opens no page (D24)', () => {
    const own = { needId: 'a:own', bot: 'mira', where: 'bot' as const, stamped: false }
    // Behind the board at Mira, the worker's approval grabs its own board; a question opens Mira's page as its line says.
    expect(behindTakenBoard('a:worker', 'mira', [own])).toBe(false)
    expect(lineDoes({ opens: 'bot' }, undefined, false, false, true, behindTakenBoard('a:worker', 'mira', [own]))).toBe('take')
    for (const taken of [{ ...own, where: 'held' as const }, { ...own, where: 'floor' as const }, { ...own, stamped: true }]) {
      expect(behindTakenBoard('a:worker', 'mira', [taken])).toBe(true)
      // The grab is refused there (`grabAsk` gives null), and the line no longer falls through to Mira's page.
      expect(lineDoes({ opens: 'bot' }, undefined, false, false, false, behindTakenBoard('a:worker', 'mira', [taken]))).toBe('none')
      expect(lineDoes({ opens: 'bot' }, undefined, false, false, false, behindTakenBoard('q:worker', 'mira', [taken]))).toBe('none')
      // The board's own line, and another Bot's ask, are not behind it.
      expect(behindTakenBoard('a:own', 'mira', [taken])).toBe(false)
      expect(behindTakenBoard('a:juno', 'juno', [taken])).toBe(false)
    }
  })

  it('waits for the board that is about to be made, and does not open the Bot page beside it', () => {
    // The read brought the line, and the board of that ask is made a moment later: the press waits for it.
    expect(lineDoes({ opens: 'bot' }, undefined, true)).toBe('wait')
    expect(lineDoes({ opens: 'view' }, undefined, true)).toBe('wait')
    // Once the board is there, where it is decides.
    expect(lineDoes({ opens: 'bot' }, 'bot', true)).toBe('take')
    expect(lineDoes({ opens: 'bot' }, 'held', true)).toBe('none')
  })

  it('never waits for a board that will not come: an ask that a board answered and the list still names', () => {
    const need = (id: string, ts: number) => ({ id, kind: 'approval', requestId: `r-${id}`, ts }) as XrNeed
    const mira = { slug: 'mira', needs: [need('old', 1), need('new', 2)] }
    const none = new Set<string>()
    // The Bot's oldest approval gets the board, when its body is drawn and it holds no board.
    expect(boardComes('old', mira, true, false, none, boardAskOf)).toBe(true)
    expect(boardComes('new', mira, true, false, none, boardAskOf)).toBe(false)
    expect(boardComes('old', mira, false, false, none, boardAskOf)).toBe(false)
    expect(boardComes('old', mira, true, true, none, boardAskOf)).toBe(false)
    expect(boardComes('old', undefined, true, false, none, boardAskOf)).toBe(false)
    // He stamped `old` and threw its board; the list still names it. No board comes for it, and the next one's does.
    const answered = new Set(['old'])
    expect(boardComes('old', mira, true, false, answered, boardAskOf)).toBe(false)
    expect(boardComes('new', mira, true, false, answered, boardAskOf)).toBe(true)
    // Its line does nothing, so it gets no target; it does not wait, and it does not open the Bot's page.
    expect(lineDoes({ opens: 'bot' }, undefined, false, true)).toBe('none')
    expect(lineDoes({ opens: 'bot' }, undefined, true, true)).toBe('none')
    // The same rule as the boards' own: what the hook's rule makes a board for is what the line waits for.
    const made = boardsNow(new Map(), [mira], answered)
    expect([...made.boards.keys()]).toEqual(['new'])
  })

  it('treats an id that was answered, left the list and is asked again as a new ask: its line waits or takes', async () => {
    let clock = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => clock)
    const ask = (id: string): XrNeed => ({
      id, kind: 'approval', card: true, bot: 'mira', who: 'Mira', topic: 'release', slot: 's1', action: 'Run the release script', tool: 'shell',
      command: 'npm run release', extra: [], version: `${id}|m1|npm run release`, purpose: '', origin: 'native', requestId: `r-${id}`, requestMid: 'm1', questions: [], ts: 1,
    })
    const mira = (needs: XrNeed[]) => ({ slug: 'mira', label: 'Mira', needs }) as unknown as XrBot
    const { result, rerender } = renderHook(({ crew }) => useBoards(crew, { say: () => {}, openBot: () => {} }, () => Promise.resolve('approved' as const)), { initialProps: { crew: [mira([ask('a')])] } })
    /** What the room works out for the line of `a` (`Workspace.tsx`, `lineDoesNow`), from the hook's own book. */
    const does = (crew: XrBot[]) => {
      const views = result.current.views
      const answered = result.current.answered()
      const awaited = boardComes('a', crew[0], true, views.some(v => v.bot === 'mira'), answered, boardAskOf)
      return lineDoes({ opens: 'bot' }, views.find(v => v.needId === 'a')?.where, awaited, answered.has('a'))
    }
    act(() => { result.current.take('a') })
    clock += BOARD.calmMs + 1
    await act(async () => { result.current.stamp('a', { u: 0.6, v: 0.6 }) })
    act(() => { result.current.release('a', { px: 380, pxPerMs: 4, lastMs: 8, onBot: false }) })
    // Answered and still listed: no board comes, and the line does nothing.
    expect(does([mira([ask('a')])])).toBe('none')
    // It leaves the list, and the same id is asked again: a new ask.
    rerender({ crew: [mira([])] })
    const again = [mira([ask('a')])]
    // Before the hook has read the new list, the line waits for the board; once the board is made, a press takes it.
    expect(does(again)).toBe('wait')
    rerender({ crew: again })
    expect(does(again)).toBe('take')
  })

  it('opens the ask that a line names, and takes no press where that ask can be opened nowhere', () => {
    // A card on the Bot's own page: an approval that stands on a session's row, a question.
    expect(lineOpens({ bot: 'atlas', kind: 'approval', card: true })).toBe('bot')
    expect(lineOpens({ bot: 'juno', kind: 'question', card: true })).toBe('bot')
    // A Bot that is only flagged as waiting is read on its own page.
    expect(lineOpens({ bot: 'juno', kind: 'attention', card: false })).toBe('bot')
    // An approval that only the approvals list carries has no card on the Bot's page: the page of every session's asks.
    expect(lineOpens({ bot: 'atlas', kind: 'approval', card: false })).toBe('view')
    // Work that no Bot owns: its ask is on that page too; with no request read it can be opened nowhere.
    expect(lineOpens({ bot: '', kind: 'approval', card: true })).toBe('view')
    expect(lineOpens({ bot: '', kind: 'question', card: true })).toBe('view')
    expect(lineOpens({ bot: '', kind: 'attention', card: false })).toBe(null)
    // Two asks of one Bot go to two places when only one of them has its card on the Bot's page.
    const crew = buildXrCrew(xrFixture(false))
    const atlas = homeLines(crew.needs, 9).lines.filter(l => l.bot === 'atlas')
    expect(atlas.map(l => l.opens).sort()).toEqual(['bot', 'view'])
  })
})

describe('where the Home surface stands', () => {
  it('is large while no page shows and compact while one does', () => {
    expect(homeForm(0)).toBe('large')
    expect(homeForm(1)).toBe('compact')
  })

  it('stands level and grows by a row for each line of what waits', () => {
    const none = homeLayout('large', 0, false)
    const three = homeLayout('large', 3, true)
    expect(none.pitchDeg).toBe(0)
    expect(none.lines).toHaveLength(0)
    expect(three.lines).toHaveLength(3)
    expect(three.heightM - none.heightM).toBeCloseTo(4 * HOME.large.lineM + HOME.large.gapM, 6)
    // Every target is inside the plate, and no two overlap.
    const inside = (r: { cx: number; cy: number; w: number; h: number }) => Math.abs(r.cx) + r.w / 2 <= three.widthM / 2 + 1e-9 && Math.abs(r.cy) + r.h / 2 <= three.heightM / 2 + 1e-9
    const rects = [...three.lines, three.more!, three.switch]
    expect(rects.every(inside)).toBe(true)
    for (let i = 1; i < rects.length; i += 1) expect(rects[i - 1].cy - rects[i - 1].h / 2).toBeGreaterThanOrEqual(rects[i].cy + rects[i].h / 2 - 1e-9)
    // A press target is no smaller than the angle a hand can hold a ray on.
    const least = HOME.distanceM * Math.tan((PAGES.edgeDeg * Math.PI) / 180)
    expect(Math.min(...rects.map(r => r.h))).toBeGreaterThan(least)
  })

  it('stands as a strip above the first page when asked to, off the page\'s own bar', () => {
    const strip = homeLayout('compact', 3, true, true)
    const RAD = Math.PI / 180
    const half = Math.atan2(strip.heightM / 2, strip.distanceM) / RAD
    const pageTopDeg = firstPageTop().pageTop / RAD
    // Whole in the lens (D70 1, D73): its top at most 18.9 degrees up. A first page opens 1.4 degrees under level
    // (D74), so the strip's lower edge is never under the page's top edge: it lies over the band that takes that edge.
    expect(PAGES.lowerDeg).toBe(-1.4)
    expect(strip.pitchDeg + half).toBeLessThanOrEqual(STRIP_TOP_MOST_DEG + 1e-9)
    expect(strip.pitchDeg - half).toBeLessThan(pageTopDeg + PAGES.edgeDeg)
    expect(strip.pitchDeg - half).toBeGreaterThanOrEqual(pageTopDeg)
  })

  it('opens a first page low enough for the strip and high enough to be whole in the lens (D74)', () => {
    const RAD = Math.PI / 180
    const halfDeg = Math.atan2((PAGES.cssHeight * M_PER_PX) / 2, PAGES.distanceM) / RAD
    expect(firstPageTop().pageTop / RAD).toBeCloseTo(15.95, 1)
    // Its lower edge, 18.75 down: inside the lens's 20.35 by more than a degree.
    expect(PAGES.lowerDeg - halfDeg).toBeGreaterThan(-20.35 + 1)
  })

  it('keeps its top corners in the lens with the head turned 10 degrees either way (D73)', () => {
    const strip = homeLayout('compact', 3, true, true)
    const RAD = Math.PI / 180
    // The top edge's height over the eye and its depth, the strip facing straight ahead and tilted to face the eye; the
    // head turned by psi sees its far corner at a height of y / (d cos psi - x sin psi) on the lens.
    const p = strip.pitchDeg * RAD
    const d = strip.distanceM * Math.cos(p) - (strip.heightM / 2) * Math.sin(p)
    const y = strip.distanceM * Math.sin(p) + (strip.heightM / 2) * Math.cos(p)
    const x = strip.widthM / 2
    const cornerDeg = (deg: number) => Math.atan(y / (d * Math.cos(deg * RAD) - x * Math.sin(deg * RAD))) / RAD
    // Under the lens's top to 9.5 degrees; at 10 within a pixel of it (0.045 degrees on the Aura's 900 rows), and
    // just past it the end leaves the lens sideways anyway (D74).
    expect(cornerDeg(9.5)).toBeLessThanOrEqual(20.35)
    expect(cornerDeg(10)).toBeLessThanOrEqual(20.35 + 0.045)
  })

  it('stands as a strip in front of the first page uppermost rows, in view of a level look', () => {
    const strip = homeLayout('compact', 3, true)
    const RAD = Math.PI / 180
    const half = Math.atan2(strip.heightM / 2, strip.distanceM) / RAD
    const { pageTop, lineM } = firstPageTop()
    const pageTopDeg = pageTop / RAD
    // Its upper edge is on the line of sight to the page's upper edge: the page's frame and close mark stay clear.
    expect(strip.pitchDeg + half).toBeCloseTo(pageTopDeg, 6)
    // The glasses show 20.35 degrees up from a level look (40.7 in all): the whole strip is inside, with air.
    expect(strip.pitchDeg + half).toBeLessThan(20.35 - 2.5)
    // It is in front of the page on that line, and covers no more of the page than its top bar (60 CSS px).
    expect(strip.distanceM).toBeLessThan(lineM)
    const coveredPx = ((pageTopDeg - (strip.pitchDeg - half)) * RAD * lineM) / M_PER_PX
    expect(coveredPx).toBeLessThan(62)
    expect(strip.lines).toHaveLength(0)
    expect(Math.abs(strip.switch.cx) + strip.switch.w / 2).toBeLessThanOrEqual(strip.widthM / 2)
    // The switch is no smaller than the angle a hand can hold a ray on.
    expect(strip.switch.h).toBeGreaterThan(strip.distanceM * Math.tan(PAGES.edgeDeg * RAD))
  })
})

describe('the switch in the room record', () => {
  const chat: KeptPage = { path: '/chat', at: { yawDeg: 0, pitchDeg: PAGES.lowerDeg, distanceM: 1.75 }, css: [1024, 720], byHand: false }

  it('is kept only while the pages are put away', () => {
    expect(pagesHiddenAtStart(writeRoom([chat], false, null, {}, true))).toBe(true)
    expect(pagesHiddenAtStart(writeRoom([chat]))).toBe(false)
    expect(writeRoom([chat])).not.toContain('pagesHidden')
  })

  it('opens a room with no record kept with its pages put away: Home is the main panel', () => {
    expect(pagesHiddenAtStart(null)).toBe(true)
    expect(pagesHiddenAtStart('')).toBe(true)
    // What is no record of this version is no room kept.
    expect(pagesHiddenAtStart('not json')).toBe(true)
    expect(pagesHiddenAtStart('{"v":2,"pages":[]}')).toBe(true)
    expect(pagesHiddenAtStart('{"v":1}')).toBe(true)
  })

  it('reads pages=show only where no record is kept', () => {
    // No record, or what is no record of this version: the word opens the room with its pages showing.
    expect(pagesHiddenAtStart(null, true)).toBe(false)
    expect(pagesHiddenAtStart('', true)).toBe(false)
    expect(pagesHiddenAtStart('not json', true)).toBe(false)
    expect(pagesHiddenAtStart('{"v":2,"pages":[]}', true)).toBe(false)
    // A kept room opens as it was kept, whatever the address says.
    expect(pagesHiddenAtStart(writeRoom([chat], false, null, {}, true), true)).toBe(true)
    expect(pagesHiddenAtStart(writeRoom([chat]), true)).toBe(false)
    expect(pagesHiddenAtStart(writeRoom([chat]), false)).toBe(false)
  })

  it('opens a record of an older build, which has no such field, with its pages showing', () => {
    const old = JSON.stringify({ v: 1, walls: true, pages: [chat] })
    expect(pagesHiddenAtStart(old)).toBe(false)
    expect(readRoom(old)).toEqual([chat])
    expect(readWalls(old)).toBe(true)
    // A kept room with no page left is a kept room too.
    expect(pagesHiddenAtStart('{"v":1,"pages":[]}')).toBe(false)
  })

  it('leaves the pages as an older build reads them', () => {
    const text = writeRoom([chat], false, null, {}, true)
    expect(readRoom(text)).toEqual([chat])
    expect(pagesHiddenAtStart('{"v":1,"pagesHidden":"yes","pages":[]}')).toBe(false)
  })
})

describe('the since line over large Home (REVIEW-15 S1)', () => {
  it('lays Home out with its filed lines, so the line stands over them, not inside', () => {
    const home = { done: [1, 2, 3, 4, 5], toRead: 0 }
    expect(homeDoneRows(home)).toEqual({ head: true, lines: 5 })
    expect(homeDoneRows({ done: [], toRead: 2 })).toEqual({ head: true, lines: 0 })
    expect(homeDoneRows({ done: [], toRead: 0 })).toEqual({ head: false, lines: 0 })
    const big = homeLayout('large', 2, false, false, homeDoneRows(home))
    const top = big.pitchDeg + (Math.atan2(big.heightM / 2, big.distanceM) * 180) / Math.PI
    const lineLow = sinceLinePitch(big.pitchDeg, big.heightM, big.distanceM) - (Math.atan2(0.03, big.distanceM) * 180) / Math.PI
    expect(lineLow).toBeGreaterThan(top)
  })
})
