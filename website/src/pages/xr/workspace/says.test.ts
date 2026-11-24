import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../../../types'
import type { XrNeed } from '../model'
import { boardsNow, handBack, takeWords, throwBoard, wordsBoard, type Board } from './board'
import { SAYS, answersOf, botSpokeLast, capLines, cutToRaise, errorWords, pinStands, retryStands, whatToRaise, doneRows, nextSheet, onlyAck, proseOf, readMark, saysId, sessionToRaise, sheetsOf, slotOfSays, toRead, type DoneFoot, type Says } from './says'

const msg = (role: string, content: string, ts = ''): ChatMessage => ({ role, content, cls: role, ts })
/** One word a line, so a sheet's lines are its words. */
const wrap = { head: (s: string) => (s ? [s] : []), body: (s: string) => s.split(/\s+/).filter(Boolean) }

describe('a Bot\'s words, read from its own thread', () => {
  it('reads only the Bot\'s words, never his own last message (T2)', () => {
    const a = answersOf([msg('assistant', 'Old answer.', 't1'), msg('user', 'Add pagination please.', 't2'), msg('assistant', 'Keep the old shape working. Then add the new one.', 't3'), msg('user', 'Thanks, that is all.', 't4')])
    expect(a.map(x => x.first)).toEqual(['Keep the old shape working.', 'Old answer.'])
    expect(a[0].rest).toBe('Then add the new one.')
    // A thread whose last word is his has the Bot's last answer first, not his message.
    expect(a[0].key).toBe('t3|2')
    expect(answersOf([msg('user', 'Hello there.')])).toEqual([])
  })

  it('takes code blocks and tables out of the words, and counts them', () => {
    const p = proseOf('Use a `cursor`.\n\n```ts\nconst a = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nThat is all.')
    expect(p).toEqual({ prose: 'Use a cursor.\n\nThat is all.', code: 1, tables: 1, headings: [], choices: [] })
    expect(proseOf('Hi.\n\n## The plan\n\nStep one.').headings).toEqual(['The plan'])
  })

  it('holds up by itself the unread session at rest that ended first, and none while it works (T1, P3)', () => {
    const rest = [{ slot: 'own', ts: 30, unread: true }, { slot: 'worker', ts: 20, unread: true }, { slot: 'read', ts: 10, unread: false }]
    expect(sessionToRaise({ state: 'idle', rest })).toBe('worker')
    expect(sessionToRaise({ state: 'needs', rest })).toBe('worker')
    expect(sessionToRaise({ state: 'working', rest })).toBe('')
    expect(sessionToRaise({ state: 'idle', rest: [rest[2]] })).toBe('')
    // Handed back unread (D35): kept for later, and held up again only once that session changed.
    expect(sessionToRaise({ state: 'idle', rest }, new Map([['worker', 20]]))).toBe('own')
    expect(sessionToRaise({ state: 'idle', rest: [{ slot: 'worker', ts: 25, unread: true }] }, new Map([['worker', 20]]))).toBe('worker')
    // Home counts every answer that waits at its Bot, of every Bot, ten at once too.
    expect(toRead([{ rest }, { rest: [rest[0]] }])).toBe(3)
    expect(toRead(Array.from({ length: 10 }, (_, i) => ({ rest: [{ slot: `s${i}`, ts: 1, unread: true }] })))).toBe(10)
  })
})

describe('the failed sheet (P3 slice 2, D35)', () => {
  it('comes for the oldest red cut he did not put aside, before the words, and not while the Bot works', () => {
    const cuts = [{ slot: 'w2', id: 'w2@2', red: true, ts: 20 }, { slot: 'own', id: 'own@1', red: true, ts: 10 }, { slot: 'w3', id: 'w3@3', red: false, ts: 5 }]
    expect(cutToRaise({ state: 'failed', cuts })).toEqual({ slot: 'own', id: 'own@1' })
    expect(cutToRaise({ state: 'failed', cuts }, new Set(['own@1']))).toEqual({ slot: 'w2', id: 'w2@2' })
    expect(cutToRaise({ state: 'working', cuts })).toBeNull()
    // A dim cut (seen) brings none; a new cut of the same session is a new id, so a new sheet.
    expect(cutToRaise({ state: 'idle', cuts: [cuts[2]] })).toBeNull()
    expect(cutToRaise({ state: 'failed', cuts: [{ ...cuts[1], id: 'own@9' }] }, new Set(['own@1']))).toEqual({ slot: 'own', id: 'own@9' })
  })

  it('says what failed: the error\'s message large, its kind and code small; else the text as it is', () => {
    expect(errorWords("Prompt error: {'code': -32603, 'message': 'fake ACP backend: injected failure'}")).toEqual({ line: 'fake ACP backend: injected failure', small: 'Prompt error · -32603' })
    expect(errorWords('Error: {"message": "it\'s gone", "code": 7}')).toEqual({ line: "it's gone", small: 'Error · 7' })
    expect(errorWords('The session ended.\n  Try later.')).toEqual({ line: 'The session ended. Try later.', small: '' })
    expect(capLines(['a', 'b', 'c', 'd'], 3)).toEqual(['a', 'b', 'c\u2026'])
    expect(capLines(['a', 'b'], 3)).toEqual(['a', 'b'])
  })
})

describe('the done sheet (P3, D35)', () => {
  const foot: DoneFoot = { head: 'Juno: done · 10:42', short: 'done · 10:42', made: [{ text: 'PR #7 · open · CI passed', state: 'open' }], left: ['Left: write the tests'] }
  const words = (n: number) => Array.from({ length: n }, (_, i) => `w${i}`).join(' ')

  it('keeps rows on the newest answer\'s first sheet for its head, its links and what it left open, and only there', () => {
    const plain = sheetsOf([{ key: 'k2', first: 'First.', rest: words(20), code: 0, tables: 0 }, { key: 'k1', first: 'Before.', rest: '', code: 0, tables: 0 }], wrap)
    const done = sheetsOf([{ key: 'k2', first: 'First.', rest: words(20), code: 0, tables: 0 }, { key: 'k1', first: 'Before.', rest: '', code: 0, tables: 0 }], wrap, foot)
    expect(done[0].done).toEqual(foot)
    expect(done[0].head.length + done[0].body.length + doneRows(foot)).toBe(SAYS.lines)
    expect(plain[0].body.length - done[0].body.length).toBe(doneRows(foot))
    // No word is lost: it goes onto the next sheets; the turn before carries no foot.
    const plainWords = (sheets: typeof done) => sheets.filter(s => !s.before).flatMap(s => s.body).map(l => l.replace(/\u2026$/, ''))
    expect(plainWords(done)).toEqual(plainWords(plain))
    expect(done.filter(s => s.done)).toHaveLength(1)
  })

  it('ends a sheet with an ellipsis when its sentence goes on to the next sheet, and not when it ends there', () => {
    const lines = { head: (s: string) => (s ? [s] : []), body: (s: string) => s.split('\n') }
    const cut = sheetsOf([{ key: 'k', first: 'First.', rest: 'a\nb\nc\nd\ne\nf\nNothing changes\nfor a client.', code: 0, tables: 0 }], lines)
    expect(cut[0].body.at(-1)).toBe('Nothing changes\u2026')
    expect(cut[1].body[0]).toBe('for a client.')
    const ends = sheetsOf([{ key: 'k', first: 'First.', rest: 'a\nb\nc\nd\ne\nf\nIt ends here.\nThe next one.', code: 0, tables: 0 }], lines)
    expect(ends[0].body.at(-1)).toBe('It ends here.')
    // A paragraph that ends at the sheet's foot (a blank line follows) is not cut.
    const para = sheetsOf([{ key: 'k', first: 'First.', rest: 'a\nb\nc\nd\ne\nf\nno stop\n\nNext.', code: 0, tables: 0 }], lines)
    expect(para[0].body.at(-1)).toBe('no stop')
  })

  it('is a plain words board for a turn that only says "ok"', () => {
    const none = { made: [], left: [] }
    const a = (first: string, rest = '') => ({ key: 'k', first, rest, code: 0, tables: 0 })
    expect(onlyAck(a('Ok.'), none)).toBe(true)
    expect(onlyAck(a('Done, thanks!'), none)).toBe(true)
    expect(onlyAck(a('好的。'), none)).toBe(true)
    expect(onlyAck(a('Here is a short draft for the beta notes.'), none)).toBe(false)
    expect(onlyAck(a('Ok.', 'And one more thing.'), none)).toBe(false)
    expect(onlyAck(a('Ok.'), { made: [1], left: [] })).toBe(false)
    expect(onlyAck(a('Ok.'), { made: [], left: ['x'] })).toBe(false)
  })

  it('reads the session of its board from the board\'s id', () => {
    expect(slotOfSays(saysId('chat-1'))).toBe('chat-1')
    expect(slotOfSays('a:chat-1:r1')).toBe('')
  })
})

describe('the sheets of an answer', () => {
  const words = (n: number) => Array.from({ length: n }, (_, i) => `w${i}`).join(' ')

  it('has 8 lines at most on a sheet, the first sentence on the first, and says which sheet of how many (T4)', () => {
    const sheets = sheetsOf([{ key: 'k', first: 'First.', rest: words(20), code: 0, tables: 0 }], wrap)
    expect(sheets.map(s => s.head.length + s.body.length)).toEqual([8, 8, 5])
    expect(sheets[0].head).toEqual(['First.'])
    expect(sheets.map(s => `${s.n}/${s.of}`)).toEqual(['1/3', '2/3', '3/3'])
    expect(sheets.every(s => s.left === null)).toBe(true)
  })

  it('never leaves a heading as a sheet\'s last line: it goes on with its words', () => {
    // Sheet 1 holds the first sentence and 7 lines: the heading would be its 7th body line.
    const rest = ['l1', 'l2', 'l3', 'l4', 'l5', '', 'The plan', 'step', 'more'].join(' \n ')
    const lines = { head: (s: string) => (s ? [s] : []), body: (s: string) => s.split('\n').map(x => x.trim()) }
    const sheets = sheetsOf([{ key: 'k', first: 'First.', rest, code: 0, tables: 0, headings: ['The plan'] }], lines)
    expect(sheets[0].body.at(-1)).toBe('l5')
    expect(sheets[1].body.slice(0, 2)).toEqual(['The plan', 'step'])
  })

  it('turns to the next sheet, then the turn before, then the first again (T5)', () => {
    const sheets = sheetsOf([{ key: 'k', first: 'New.', rest: words(10), code: 0, tables: 0 }, { key: 'j', first: 'Old.', rest: '', code: 0, tables: 0 }], wrap)
    expect(sheets.map(s => `${s.before ? 'before ' : ''}${s.n}/${s.of}`)).toEqual(['1/2', '2/2', 'before 1/1'])
    expect([0, 1, 2].map(i => nextSheet(i, sheets.length))).toEqual([1, 2, 0])
  })

  it('ends an answer past 5 sheets, or with code or a table, on a sheet that counts what is left out (T6)', () => {
    const long = sheetsOf([{ key: 'k', first: 'First.', rest: words(7 + SAYS.lines * 4 + 30), code: 2, tables: 1 }], wrap)
    expect(long).toHaveLength(SAYS.sheets + 1)
    expect(long[SAYS.sheets]).toMatchObject({ head: [], body: [], n: 6, of: 6, left: { words: 27, tables: 1, code: 2 } })
    // The cap cut it: the last lines of its last paragraph stand on the count sheet.
    expect(long[SAYS.sheets].tail).toHaveLength(3)
    // A short answer with a table ends on the count too; a short plain one has none.
    expect(sheetsOf([{ key: 'k', first: 'First.', rest: '', code: 0, tables: 1 }], wrap).map(s => s.left)).toEqual([null, { words: 0, tables: 1, code: 0 }])
    expect(sheetsOf([{ key: 'k', first: 'First.', rest: '', code: 0, tables: 0 }], wrap).map(s => s.left)).toEqual([null])
  })
})

describe('a words board on a Bot\'s clipboard', () => {
  const need = (id: string): XrNeed => ({ id, kind: 'approval', card: true, bot: 'mira', who: 'Mira', topic: '', slot: 's1', action: 'Run it', tool: 'shell', command: 'npm run release', extra: [], version: `${id}|1`, purpose: '', origin: 'native', requestId: `r-${id}`, requestMid: 'm1', questions: [], ts: 1 })
  const says = (over: Partial<Says> = {}): Says => ({ id: saysId('s1'), version: 'v1', slot: 's1', raise: true, state: 'ok', sentence: 'Done.', sheets: [], ...over })
  const bot = (needs: XrNeed[] = []) => [{ slug: 'mira', needs }]

  it('comes up when its words are raised, and an approval of its Bot comes first (T1, T8)', () => {
    const up = boardsNow(new Map(), bot(), new Set(), undefined, new Map([['mira', says()]])).boards
    expect([...up.values()].map(b => [b.needId, b.kind])).toEqual([[saysId('s1'), 'words']])
    // An approval: its board comes, and the words board at the Bot steps back quietly for it.
    const next = boardsNow(up, bot([need('a')]), new Set(), undefined, new Map([['mira', says()]]))
    expect([...next.boards.keys()]).toEqual(['a'])
    expect(next.gone).toEqual([])
    // Answered: the words come again after it.
    expect([...boardsNow(new Map(), bot(), new Set(), undefined, new Map([['mira', says()]])).boards.keys()]).toEqual([saysId('s1')])
  })

  it('stays in his hand when an approval comes; the approval waits behind it', () => {
    const held = new Map([[saysId('s1'), takeWords(wordsBoard(says(), 'mira'), 0)]])
    expect([...boardsNow(held, bot([need('a')]), new Set(), undefined, new Map([['mira', says()]])).boards.keys()]).toEqual([saysId('s1')])
  })

  it('sends nothing when thrown: it falls apart; a board at its Bot goes once its words are read (T7)', () => {
    const b = takeWords(wordsBoard(says(), 'mira'), 0)
    expect(throwBoard(b, 5)).toEqual({ board: null, particles: true })
    // Read now (no longer raised), a words board at the Bot goes: the Bot lowers its clipboard.
    expect([...boardsNow(new Map([[b.needId, wordsBoard(says(), 'mira')]]), bot(), new Set(), undefined, new Map([['mira', says({ raise: false })]])).boards.keys()]).toEqual([])
  })

  it('holds one session\'s words at a time: a board at the Bot of another session goes, the one it holds now comes (P3)', () => {
    const at = new Map([[saysId('s1'), wordsBoard(says(), 'mira')]])
    const next = boardsNow(at, bot(), new Set(), undefined, new Map([['mira', says({ id: saysId('w2'), slot: 'w2', version: 'v9' })]])).boards
    expect([...next.keys()]).toEqual([saysId('w2')])
    // One in his hand stays with the sheets it was taken with.
    const held = new Map([[saysId('s1'), takeWords(wordsBoard(says(), 'mira'), 0)]])
    expect([...boardsNow(held, bot(), new Set(), undefined, new Map([['mira', says({ id: saysId('w2'), slot: 'w2' })]])).boards.keys()]).toEqual([saysId('s1')])
  })

  it('stands in place of an ask board he handed back, as a question comes past it; that ask waits behind (D27)', () => {
    const askBack: Board = { ...handBack(takeWords(wordsBoard({ id: 'a', version: 'a|1' }, 'mira'), 0), true), kind: 'ask' }
    const words = new Map([['mira', says()]])
    const first = boardsNow(new Map([['a', askBack]]), bot([need('a')]), new Set(), undefined, words).boards
    expect([...first.values()].map(b => [b.needId, b.over])).toEqual([[saysId('s1'), 'a']])
    // On the next read it stays: the approval it stands over does not push it back.
    expect([...boardsNow(first, bot([need('a')]), new Set(), undefined, words).boards.keys()]).toEqual([saysId('s1')])
    // A board he has not taken holds the words behind it.
    const untaken = boardsNow(new Map(), bot([need('a')]), new Set(), undefined, words).boards
    expect([...untaken.keys()]).toEqual(['a'])
  })
})

describe('a Bot that has not answered yet', () => {
  it('holds up nothing by itself after his own message: the Bot speaks last or not at all', () => {
    expect(botSpokeLast([msg('user', 'Hi.'), msg('assistant', 'Hello.')])).toBe(true)
    expect(botSpokeLast([msg('assistant', 'Hello.'), msg('user', 'Which step comes first?')])).toBe(false)
    expect(botSpokeLast([msg('assistant', 'Hello.'), msg('tool', 'ran')])).toBe(true)
  })
})

describe('a words board in his hand (review M1)', () => {
  const sheet = (word: string) => ({ head: [word], body: [], n: 1, of: 1, before: false, left: null })
  const says = (version: string, word: string): Says => ({ id: saysId('s1'), version, slot: 's1', raise: true, state: 'ok', sentence: word, sheets: [sheet(word)] })
  const crew = [{ slug: 'mira', needs: [] }]

  it('keeps the sheets it was taken with while a newer answer lands, and the newer one comes as the next board', () => {
    const v1 = says('2026-11-21T01:00:00Z|3', 'Old')
    const up = boardsNow(new Map(), crew, new Set(), undefined, new Map([['mira', v1]])).boards
    const held = new Map([...up].map(([id, b]) => [id, takeWords(b, 0, v1)]))
    const v2 = says('2026-11-21T01:05:00Z|5', 'New')
    const after = boardsNow(held, crew, new Set(), undefined, new Map([['mira', v2]])).boards
    const board = [...after.values()][0]
    expect(board).toMatchObject({ where: 'held', version: v1.version })
    expect(board.sheets.map(s => s.head[0])).toEqual(['Old'])
    // Thrown: the board is gone, and the newer answer is raised as the next board.
    const next = boardsNow(new Map(), crew, new Set(), undefined, new Map([['mira', v2]])).boards
    expect([...next.values()].map(b => [b.kind, b.where, b.version])).toEqual([['words', 'bot', v2.version]])
  })

  it('marks read up to the shown answer\'s own message, and here only when nothing newer came', () => {
    expect(readMark('2026-11-21T01:00:00Z|3', '2026-11-21T01:05:00Z')).toEqual({ ts: '2026-11-21T01:00:00Z', local: false })
    expect(readMark('2026-11-21T01:05:00Z|5', '2026-11-21T01:05:00Z')).toEqual({ ts: '2026-11-21T01:05:00Z', local: true })
    expect(readMark('failed', '2026-11-21T01:05:00Z')).toBeNull()
    expect(readMark('none', undefined)).toBeNull()
  })
})

describe('what asks him to act is never cut away (review M2)', () => {
  // The reviewer's wrap: about 40 characters a head line, 60 a body line.
  const wrap60 = { head: (s: string) => (s ? s.match(/.{1,40}(\s|$)/g) ?? [s] : []), body: (s: string) => s.split('\n').flatMap(p => (p ? p.match(/.{1,60}(\s|$)/g) ?? [p] : [''])) }

  it('keeps the choices a Bot offers him, on the sheet where the answer ends', () => {
    const answers = answersOf([msg('user', 'go', '1'), msg('assistant', 'I can ship the fix now or wait for the review.\n\n[OPTIONS: Ship it now | Wait for review]', '2')])
    expect(answers[0].choices).toEqual(['Ship it now', 'Wait for review'])
    const sheets = sheetsOf(answers, wrap60)
    expect(sheets.at(-1)?.choices).toEqual(['Ship it now', 'Wait for review'])
    expect(sheets.filter(s => s.choices).length).toBe(1)
  })

  it('puts the last paragraph of an answer that the cap cut on its count sheet, above the counts', () => {
    const long = Array.from({ length: 30 }, (_, i) => `Step ${i + 1} of the plan does one small and clear thing to the code.`).join('\n\n') + '\n\nShall I go ahead and merge it?'
    const sheets = sheetsOf(answersOf([msg('user', 'go', '1'), msg('assistant', long, '2')]), wrap60)
    const end = sheets.at(-1)
    expect(end?.left?.words).toBeGreaterThan(0)
    expect(end?.tail?.join(' ')).toContain('Shall I go ahead and merge it?')
    // It is said once: the count sheet's words leave out the paragraph it shows.
    expect(sheets.flatMap(s => [...s.head, ...s.body, ...(s.tail ?? [])]).join(' ').match(/Shall I go ahead/g)).toHaveLength(1)
  })
})

describe('the choices a Bot offers him, as boxes', () => {
  const lines = { head: (s: string) => (s ? [s] : []), body: (s: string) => s.split(/\s+/).filter(Boolean) }
  it('stand on the last sheet when they fit, else on a sheet of their own', () => {
    const fit = sheetsOf([{ key: 'k', first: 'First.', rest: 'a b', code: 0, tables: 0, choices: ['Yes', 'No'] }], lines)
    expect(fit.map(s => s.choices ?? null)).toEqual([['Yes', 'No']])
    const full = sheetsOf([{ key: 'k', first: 'First.', rest: 'a b c d e f', code: 0, tables: 0, choices: ['Yes', 'No'] }], lines)
    expect(full.map(s => [s.n, s.of, s.choices ?? null])).toEqual([[1, 2, null], [2, 2, ['Yes', 'No']]])
  })
})

describe('"Try again" only while the cut stands (REVIEW-15 P3 M1, D39 ruling 5)', () => {
  it('goes while the Bot still has that cut and does not work; else nothing is sent', () => {
    expect(retryStands('own@1', { state: 'failed', cuts: [{ id: 'own@1' }] })).toBe(true)
    // Picked up elsewhere and ended: the cut is gone.
    expect(retryStands('own@1', { state: 'idle', cuts: [] })).toBe(false)
    // Picked up elsewhere and still running.
    expect(retryStands('own@1', { state: 'working', cuts: [{ id: 'own@1' }] })).toBe(false)
    // A new cut of the same session is another sheet.
    expect(retryStands('own@1', { state: 'failed', cuts: [{ id: 'own@2' }] })).toBe(false)
    expect(retryStands('own@1', undefined)).toBe(false)
  })
})

describe('a tap\'s pin (REVIEW-15 P3 M3)', () => {
  const otto = (over: object = {}) => ({ state: 'idle', lastTs: 100, rest: [{ slot: 'own', ts: 90, unread: false }], ...over })
  it('holds while the Bot rests and nothing newer comes', () => {
    expect(pinStands({ slot: 'own', at: 100 }, otto())).toBe(true)
  })
  it('ends once the Bot works, and once a newer unread session of it comes', () => {
    expect(pinStands({ slot: 'own', at: 100 }, otto({ state: 'working' }))).toBe(false)
    expect(pinStands({ slot: 'own', at: 100 }, otto({ lastTs: 120, rest: [{ slot: 'own', ts: 90, unread: false }, { slot: 'w2', ts: 120, unread: true }] }))).toBe(false)
    // An unread session that was there when he tapped does not end it.
    expect(pinStands({ slot: 'own', at: 120 }, otto({ lastTs: 120, rest: [{ slot: 'own', ts: 90, unread: false }, { slot: 'w2', ts: 120, unread: true }] }))).toBe(true)
  })
})

describe('what a Bot holds up (REVIEW-15 P3 M3)', () => {
  const cutOf = (over: object = {}) => ({ slot: 'w2', id: 'w2@1', red: true, ts: 50, stale: false, ...over })
  const otto = { state: 'failed', lastTs: 100, rest: [{ slot: 'own', ts: 90, unread: false }], cuts: [cutOf()] }
  it('a red cut goes before a tap\'s pin', () => {
    expect(whatToRaise(otto, { slot: 'own', at: 100 }, new Set(), new Map())).toMatchObject({ slot: 'w2', auto: true, cut: { id: 'w2@1' } })
  })
  it('the pin holds when no red cut stands, and raises a dim cut of its own session', () => {
    const dim = { ...otto, cuts: [cutOf({ red: false })] }
    expect(whatToRaise(dim, { slot: 'own', at: 100 }, new Set(), new Map())).toMatchObject({ slot: 'own', auto: false, cut: null })
    expect(whatToRaise(dim, { slot: 'w2', at: 100 }, new Set(), new Map())).toMatchObject({ slot: 'w2', auto: false, cut: { id: 'w2@1' } })
    // Put aside, the red cut no longer goes before the pin.
    expect(whatToRaise(otto, { slot: 'own', at: 100 }, new Set(['w2@1']), new Map())).toMatchObject({ slot: 'own', auto: false })
  })
})

describe('only a Bot\'s own thread is to read (D39, ruling 2)', () => {
  // Mira's own thread is read; six of her workers ended with words that nobody read.
  const rest = [{ slot: 'own', ts: 10, unread: false, direct: true }, ...Array.from({ length: 6 }, (_, i) => ({ slot: `w${i}`, ts: 20 + i, unread: true, direct: false }))]
  it('a worker\'s answer raises no done sheet and counts not as to read', () => {
    expect(sessionToRaise({ state: 'idle', rest })).toBe('')
    expect(toRead([{ rest }])).toBe(0)
  })
  it('her own thread does, once it has words he has not read', () => {
    const own = [{ ...rest[0], ts: 40, unread: true }, ...rest.slice(1)]
    expect(sessionToRaise({ state: 'idle', rest: own })).toBe('own')
    expect(toRead([{ rest: own }])).toBe(1)
  })
})
