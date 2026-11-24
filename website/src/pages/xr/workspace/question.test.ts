import { describe, expect, it } from 'vitest'
import type { XrNeed } from '../model'
import { BOARD, boardAskOf, boardFor, boardsNow, floorEnds, handBack, questionLetGo, stampQuestion, takeBoard, throwBoard, turnTo } from './board'
import { QUESTION, answerOf, answerable, answersAtPrint, noteFor, pick, questionSheets } from './question'
import { noteAfterKey, noteHeard } from './useComment'

const question = (id: string, over: Partial<XrNeed> = {}): XrNeed => ({
  id, kind: 'question', card: true, bot: 'mira', who: 'Mira', topic: '', slot: 's1', action: 'Which channel?', tool: '', command: '', extra: [],
  version: `${id}|v1`, purpose: '', origin: 'native', requestId: id, requestMid: '', askId: `ask-${id}`, ts: 2,
  questions: [{ header: 'Release', question: 'Which channel does this release go to?', options: ['Stable', 'Beta'] }], ...over,
})
const approval = (id: string): XrNeed => ({ ...question(id), kind: 'approval', askId: '', requestId: `r-${id}`, questions: [], ts: 1 })
const print = { u: 0.5, v: 0.5, tilt: -0.1, seed: 1 }
const mint = () => 'resp-1'
const PARAGRAPH = '\n\n'

describe('a question as sheets with boxes', () => {
  it('is a board only when the gateway waits on it, after the Bot\'s approvals', () => {
    expect(answerable(question('q'))).toBe(true)
    expect(answerable(question('q', { askId: '' }))).toBe(false)
    expect(boardAskOf({ needs: [question('q'), approval('a')] })?.id).toBe('a')
    expect(boardAskOf({ needs: [question('q')] })?.id).toBe('q')
    expect(boardAskOf({ needs: [question('q'), approval('a')] }, true)?.id).toBe('q')
  })

  it('has one sheet a question, and more sheets for more choices than fit, never smaller boxes', () => {
    const options = Array.from({ length: QUESTION.boxes + 2 }, (_, i) => `Choice ${i + 1}`)
    const sheets = questionSheets(question('q', { questions: [{ header: '', question: 'Pick one', options }, { header: '', question: 'Why?', options: [] }] }))
    expect(sheets.map(s => [s.q, s.options.length, `${s.n}/${s.of}`])).toEqual([[0, QUESTION.boxes, '1/3'], [0, 2, '2/3'], [1, 0, '3/3']])
  })

  it('answers at the print on a box: one choice, keyed by the question\'s words, once', () => {
    const need = question('q')
    const held = takeBoard(boardFor(need), need, 0)
    const at = BOARD.calmMs + 1
    expect(stampQuestion(held, need, at, -1, print, mint).do).toBe('miss')
    expect(stampQuestion(held, need, 10, 1, print, mint).do).toBe('wait')
    const fate = stampQuestion(held, need, at, 1, print, mint)
    expect(fate).toMatchObject({ do: 'answer', answers: { 'Which channel does this release go to?': 'Beta' }, board: { stamped: true, responseId: 'resp-1', picks: { 0: ['Beta'] } } })
    if (fate.do !== 'answer') throw new Error('no answer')
    expect(fate.board.marks['0|Beta']).toEqual(print)
    // A stamped board takes no second answer.
    expect(stampQuestion(fate.board, need, at + 1, 0, print, mint).do).toBe('nothing')
  })

  it('waits for the last question, and lets a question that takes several go at the release', () => {
    const two = question('q', { questions: [{ header: '', question: 'A?', options: ['x', 'y'] }, { header: '', question: 'B?', options: ['z', 'w'] }] })
    const first = pick(two, {}, 0, 0)
    expect(answersAtPrint(two, first)).toBe(false)
    expect(answersAtPrint(two, pick(two, first, 1, 1))).toBe(true)
    const many = question('q', { questions: [{ header: '', question: 'Which?', options: ['a', 'b', 'c'], multi: true }] })
    const ab = pick(many, pick(many, {}, 0, 0), 0, 1)
    expect(ab[0]).toEqual(['a', 'b'])
    expect(pick(many, ab, 0, 0)[0]).toEqual(['b'])
    expect(answersAtPrint(many, ab)).toBe(false)
    expect(answerOf(many, ab, '', 0, [0])).toEqual({ 'Which?': 'a, b' })
  })

  it('takes his note as the answer of a question with no choice, or none chosen', () => {
    const free = question('q', { questions: [{ header: '', question: 'What should it be called?', options: [] }] })
    expect(answerOf(free, {}, '', 0, [0])).toBeNull()
    expect(answerOf(free, {}, '  Crew notes ', 0, [0])).toEqual({ 'What should it be called?': 'Crew notes' })
    expect(answerOf(question('q'), {}, 'Neither, wait a day', 0, [0])).toEqual({ 'Which channel does this release go to?': 'Neither, wait a day' })
  })

  it('takes his note as the answer of ONE question: the one on his sheet, or the only one left (review M2)', () => {
    const three = question('q', { questions: [{ header: '', question: 'Which channel?', options: ['Alpha', 'Beta'] }, { header: '', question: 'Which day?', options: ['Mon', 'Tue'] }, { header: '', question: 'Who reviews?', options: ['Ann', 'Bo'] }] })
    const one = pick(three, {}, 0, 1)
    // Two questions without a print, and he looks at the sheet of the one he printed: the note answers none of them.
    expect(noteFor(three, one, 0, [0])).toBeNull()
    expect(answerOf(three, one, 'later', 0, [0])).toBeNull()
    // On the sheet of "Which day?" the note answers that one only; "Who reviews?" still waits for a box.
    expect(noteFor(three, one, 1, [0, 1])).toBe(1)
    expect(answerOf(three, one, 'later', 1, [0, 1])).toBeNull()
    // With one question left whose sheet he has seen, the note answers it from any sheet.
    const two = pick(three, one, 2, 0)
    expect(answerOf(three, two, 'later', 0, [0, 1, 2])).toEqual({ 'Which channel?': 'Beta', 'Which day?': 'later', 'Who reviews?': 'Ann' })
  })

  it('never lets the note answer the only question left when he has not turned to its sheet (review, 07:20)', () => {
    const two = question('q', { questions: [{ header: '', question: 'A?', options: ['x', 'y'] }, { header: '', question: 'B?', options: ['z', 'w'] }] })
    const held = { ...takeBoard(boardFor(two), two, 0), picks: pick(two, {}, 0, 0), comment: 'later' }
    // On the first sheet, never turned: "B?" is the only one left, and the note does not answer it.
    expect(noteFor(two, held.picks, 0, [0])).toBeNull()
    expect(questionLetGo(held, two)).toEqual({ do: 'keep', open: 1 })
    // Turned to its sheet and back: now it does.
    const seen = turnTo(turnTo(held, 1), 0)
    expect(seen.looked).toEqual([1, 0])
    expect(questionLetGo(seen, two)).toEqual({ do: 'answer', answers: { 'A?': 'x', 'B?': 'later' } })
  })

  it('sends his note with a whole printed answer, after the choice of the question he looks at (review small 5)', () => {
    const two = question('q', { questions: [{ header: '', question: 'A?', options: ['x', 'y'] }, { header: '', question: 'B?', options: ['z', 'w'] }] })
    const both = pick(two, pick(two, {}, 0, 0), 1, 1)
    expect(answerOf(two, both, 'after lunch', 1, [0, 1])).toEqual({ 'A?': 'x', 'B?': ['w', 'after lunch'].join(PARAGRAPH) })
    expect(answerOf(two, both, '', 1, [0, 1])).toEqual({ 'A?': 'x', 'B?': 'w' })
    // At the last print the note goes with it, and its line closes.
    const held = { ...takeBoard(boardFor(two), two, 0), picks: pick(two, {}, 0, 0), sheet: 1, comment: 'after lunch', writing: true }
    const fate = stampQuestion(held, two, BOARD.calmMs + 1, 1, print, mint)
    expect(fate).toMatchObject({ do: 'answer', answers: { 'B?': ['w', 'after lunch'].join(PARAGRAPH) }, board: { writing: false } })
  })

  it('never dismisses a board with a print: short of a whole answer it goes back with its prints (review M1, D4a)', () => {
    const two = question('q', { questions: [{ header: '', question: 'A?', options: ['x', 'y'] }, { header: '', question: 'B?', options: ['z', 'w'] }] })
    const held = takeBoard(boardFor(two), two, 0)
    const printed = { ...held, picks: pick(two, {}, 0, 0) }
    expect(questionLetGo(printed, two)).toEqual({ do: 'keep', open: 1 })
    // A note on the second sheet makes the answer whole; a note that answers no one question keeps the board too.
    expect(questionLetGo({ ...printed, sheet: 1, comment: 'later' }, two)).toEqual({ do: 'answer', answers: { 'A?': 'x', 'B?': 'later' } })
    expect(questionLetGo({ ...held, comment: 'later' }, two)).toEqual({ do: 'keep', open: 1 })
    // Only a board he made nothing on is his no.
    expect(questionLetGo(held, two)).toEqual({ do: 'none' })
    // And a printed board on the floor goes back to its Bot when its time ends, never a no.
    const floor = throwBoard(printed, 0).board
    if (!floor) throw new Error('no floor')
    expect(floorEnds(floor, two, BOARD.floorMs + 1, mint)?.do).toBe('back')
    expect(floorEnds(throwBoard(held, 0).board ?? held, two, BOARD.floorMs + 1, mint)?.do).toBe('no')
  })

  it('comes up past a board he handed back, as a sheet; it waits behind one he has not taken (D24, D27)', () => {
    const crew = [{ slug: 'mira', needs: [approval('a'), question('q')] }]
    const back = handBack(takeBoard(boardFor(approval('a')), approval('a'), 0), true)
    const past = boardsNow(new Map([['a', back]]), crew).boards
    expect([...past.values()].map(b => [b.needId, b.over])).toEqual([['q', 'a']])
    expect([...boardsNow(new Map(), crew).boards.keys()]).toEqual(['a'])
  })
})

describe('his note on the paper', () => {
  it('is written by keys, Enter closes its line, and heard words join it with one space', () => {
    expect(noteAfterKey('Shi', 'p')).toBe('Ship')
    expect(noteAfterKey('Ship', 'Backspace')).toBe('Shi')
    expect(noteAfterKey('Ship', 'Enter')).toBeNull()
    expect(noteAfterKey('Ship', 'Shift')).toBe('Ship')
    expect(noteHeard('', ' ship it ')).toBe('ship it')
    expect(noteHeard('Good', 'ship it')).toBe('Good ship it')
  })
})
