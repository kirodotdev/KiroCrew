import { describe, expect, it } from 'vitest'
import { FEEDS_OK, type XrBot, type XrCrew, type XrFeeds, type XrNeed } from '../model'
import { ASKS, ASK_CLEAR_BOT_M, ASK_CLEAR_M, ASK_SESSION, ASK_SIDE_CUT_PX, askLeastFlat, askPanelFlat, askPlace, askingBots, cardsOf, facePoints, freeAsks, isBusy, keepsPanel, lensGoesBack, lensMoved, lensSetBack, newBook, nextStep, pictureFor, pressFate, pressHeldNow, pressPlaces, readAsks, settle, takeAnswers, tapAnswer, wheelFate, type AnswerBook, type AskMoment, type AskPlaceAsk, type PressFacts } from './asks'
import { M_PER_PX, PAGES } from './layout'

const need = (id: string, card = true): XrNeed => ({ id, card, kind: card ? 'approval' : 'attention' } as XrNeed)
const bot = (slug: string, ...needs: XrNeed[]): XrBot => ({ slug, name: slug, label: slug, needs } as XrBot)
const crew = (bots: XrBot[], feeds: XrFeeds = FEEDS_OK): XrCrew => ({ bots, needs: bots.flatMap(b => b.needs), unowned: 0, feeds })
const calm: AskMoment = { now: 10_000, way: 'open', calmSince: 0, panels: [], open: 1, most: 12, stopped: false }

describe('which ask is new, and which is gone', () => {
  it('counts every ask of the first whole read as seen: none of them comes up', () => {
    const book = newBook()
    expect(readAsks(book, crew([bot('mira', need('a1')), bot('juno')]))).toEqual([])
    expect(book.started).toBe(true)
    expect([...book.waiting]).toEqual([])
    expect(cardsOf(book, 'mira')).toBe(1)
    expect(nextStep(book, calm)).toEqual({ do: 'nothing' })
  })

  it('takes an ask that was not there before as new, and its Bot\'s asks as changed', () => {
    const book = newBook()
    readAsks(book, crew([bot('mira'), bot('juno')]))
    expect(readAsks(book, crew([bot('mira', need('a1')), bot('juno')]))).toEqual(['mira'])
    expect([...book.waiting]).toEqual([['a1', 'mira']])
    expect(nextStep(book, calm)).toEqual({ do: 'up', bot: 'mira' })
    // The same read again changes nothing.
    expect(readAsks(book, crew([bot('mira', need('a1')), bot('juno')]))).toEqual([])
  })

  it('brings up only an ask with a card, of a Bot: not one that only the approvals list carries, and not a stray one', () => {
    const book = newBook()
    readAsks(book, crew([bot('mira')]))
    const stray = { ...crew([bot('mira', need('x:s1', false))]), needs: [need('x:s1', false), need('a:nobody')] }
    expect(readAsks(book, stray)).toEqual([])
    expect([...book.waiting]).toEqual([])
    // It still counts for the Bot's plate.
    expect(askingBots(book)).toEqual(['mira'])
    expect(cardsOf(book, 'mira')).toBe(0)
  })

  it.each([
    ['the roster is loading', { ...FEEDS_OK, roster: 'loading' }],
    ['the approvals or the questions failed', { ...FEEDS_OK, asks: 'failed' }],
    ['the live connection is down', { ...FEEDS_OK, live: false }],
    ['the list of sessions is not loaded, or is a part of itself', { ...FEEDS_OK, rows: false }],
  ] as [string, XrFeeds][])('takes nothing as new and nothing as gone while %s', (_why, feeds) => {
    const book = newBook()
    readAsks(book, crew([bot('mira', need('a0'))]))
    readAsks(book, crew([bot('mira', need('a0'), need('a1'))]))
    expect([...book.waiting.keys()]).toEqual(['a1'])
    // A read that is not whole shows no ask at all, and one more: neither is believed.
    expect(readAsks(book, crew([bot('mira', need('a2'))], feeds))).toEqual([])
    expect([...book.waiting.keys()]).toEqual(['a1'])
    expect(cardsOf(book, 'mira')).toBe(2)
    // Whole again: the ask that arrived meanwhile is new, once; the one that went meanwhile is gone.
    expect(readAsks(book, crew([bot('mira', need('a0'), need('a2'))]))).toEqual(['mira'])
    expect([...book.waiting.keys()]).toEqual(['a2'])
    readAsks(book, crew([bot('mira', need('a0'), need('a2'))]))
    expect([...book.waiting.keys()]).toEqual(['a2'])
  })

  it('never starts from a read that is not whole: what is there at the first whole read is seen', () => {
    const book = newBook()
    readAsks(book, crew([bot('mira')], { ...FEEDS_OK, rows: false }))
    expect(book.started).toBe(false)
    readAsks(book, crew([bot('mira', need('a1'))]))
    expect([...book.waiting]).toEqual([])
  })

  it('drops an ask that is gone before its moment', () => {
    const book = newBook()
    readAsks(book, crew([bot('mira')]))
    readAsks(book, crew([bot('mira', need('a1'))]))
    expect(readAsks(book, crew([bot('mira')]))).toEqual(['mira'])
    expect(nextStep(book, calm)).toEqual({ do: 'nothing' })
  })

  it('forgets an ask that is gone: an ask of the same name later is a new ask, and is the youngest', () => {
    const book = newBook()
    readAsks(book, crew([bot('mira'), bot('juno')]))
    readAsks(book, crew([bot('mira', need('a1')), bot('juno')]))
    settle(book, 'mira')
    // Answered. The session's backend names its next ask as it named this one.
    readAsks(book, crew([bot('mira'), bot('juno')]))
    readAsks(book, crew([bot('mira'), bot('juno', need('j1'))]))
    readAsks(book, crew([bot('mira', need('a1')), bot('juno', need('j1'))]))
    expect([...book.waiting.keys()]).toEqual(['j1', 'a1'])
    expect(nextStep(book, calm)).toEqual({ do: 'up', bot: 'juno' })
    // One that was put off and still stands is not new: only one that was gone in a whole read is.
    settle(book, 'juno')
    settle(book, 'mira')
    // A read that is not whole lists no ask: nothing is taken from it, so nothing is forgotten for it.
    readAsks(book, crew([bot('mira'), bot('juno')], { ...FEEDS_OK, rows: false }))
    readAsks(book, crew([bot('mira', need('a1')), bot('juno', need('j1'))]))
    expect(book.waiting.size).toBe(0)
  })

  it('takes the ask that was seen first, whatever the model\'s order', () => {
    const book = newBook()
    readAsks(book, crew([bot('mira'), bot('juno')]))
    readAsks(book, crew([bot('mira'), bot('juno', need('j1'))]))
    // The model lists Mira first; Juno's ask was seen first.
    readAsks(book, crew([bot('mira', need('m1')), bot('juno', need('j1'))]))
    expect(nextStep(book, calm)).toEqual({ do: 'up', bot: 'juno' })
    expect(askingBots(book)).toEqual(['juno', 'mira'])
  })

  it('puts off every ask of a Bot that waits, and takes a later ask of that Bot as new', () => {
    const book = newBook()
    readAsks(book, crew([bot('mira')]))
    readAsks(book, crew([bot('mira', need('a1'), need('a2'))]))
    settle(book, 'mira')
    expect(nextStep(book, calm)).toEqual({ do: 'nothing' })
    readAsks(book, crew([bot('mira', need('a1'), need('a2'))]))
    expect(nextStep(book, calm)).toEqual({ do: 'nothing' })
    readAsks(book, crew([bot('mira', need('a1'), need('a2'), need('a3'))]))
    expect(nextStep(book, calm)).toEqual({ do: 'up', bot: 'mira' })
  })
})

describe('one board at a time for a Bot (D24)', () => {
  it('holds every other ask of a Bot whose board stands, and lets it up once the board is gone', () => {
    const waiting = new Map([['a:own', 'mira'], ['a:worker', 'mira'], ['q:worker', 'mira'], ['a:juno', 'juno']])
    // Mira's board stands for her own ask: her worker's approval and question wait behind it; Juno's ask comes up.
    expect([...freeAsks(waiting, { onBoards: new Set(['a:own']), boardBots: new Set(['mira']) }).keys()]).toEqual(['a:juno'])
    // Her board is gone: what waited comes up, as before.
    expect([...freeAsks(waiting, { onBoards: new Set(), boardBots: new Set() }).keys()]).toEqual(['a:own', 'a:worker', 'q:worker', 'a:juno'])
    // A board that is about to come for an ask holds that ask alone until its Bot counts as holding one.
    expect([...freeAsks(waiting, { onBoards: new Set(['a:worker']), boardBots: new Set() }).keys()]).toEqual(['a:own', 'q:worker', 'a:juno'])
  })

  it('lets a question past a board he handed back, and holds it behind a board he has not taken (D27)', () => {
    const waiting = new Map([['a:own', 'mira'], ['a:worker', 'mira'], ['q:worker', 'mira'], ['a:juno', 'juno']])
    const kindOf = (id: string) => (id.startsWith('q:') ? 'question' : 'approval')
    const hold = { onBoards: new Set(['a:own']), boardBots: new Set(['mira']), kindOf }
    // He handed Mira's board back: her worker's question comes up; her worker's approval still waits (one board).
    expect([...freeAsks(waiting, { ...hold, handedBack: new Set(['mira']) }).keys()]).toEqual(['q:worker', 'a:juno'])
    // Her board stands untaken: the question waits behind it too.
    expect([...freeAsks(waiting, { ...hold, handedBack: new Set() }).keys()]).toEqual(['a:juno'])
  })

  it('lets an attention ask past a board he handed back, as a question', () => {
    const waiting = new Map([['a:own', 'mira'], ['x:worker', 'mira'], ['a:worker', 'mira']])
    const kindOf = (id: string) => (id.startsWith('x:') ? 'attention' : 'approval')
    const hold = { onBoards: new Set(['a:own']), boardBots: new Set(['mira']), kindOf }
    expect([...freeAsks(waiting, { ...hold, handedBack: new Set(['mira']) }).keys()]).toEqual(['x:worker'])
    expect([...freeAsks(waiting, { ...hold, handedBack: new Set() }).keys()]).toEqual([])
  })
})

describe('when an ask comes up', () => {
  const waiting = () => {
    const book = newBook()
    readAsks(book, crew([bot('mira'), bot('juno')]))
    readAsks(book, crew([bot('mira', need('m1')), bot('juno')]))
    readAsks(book, crew([bot('mira', need('m1')), bot('juno', need('j1'))]))
    return book
  }

  it('comes up only when the gateway has said that its panels are open', () => {
    expect(nextStep(waiting(), { ...calm, way: null })).toEqual({ do: 'nothing' })
    expect(nextStep(waiting(), { ...calm, way: 'guarded' })).toEqual({ do: 'nothing' })
    expect(nextStep(waiting(), calm)).toEqual({ do: 'up', bot: 'mira' })
  })

  it('waits while the person is busy, and for two seconds after', () => {
    expect(nextStep(waiting(), { ...calm, calmSince: null })).toEqual({ do: 'nothing' })
    expect(nextStep(waiting(), { ...calm, calmSince: calm.now - ASKS.calmMs + 1 })).toEqual({ do: 'nothing' })
    expect(nextStep(waiting(), { ...calm, calmSince: calm.now - ASKS.calmMs })).toEqual({ do: 'up', bot: 'mira' })
    expect(ASKS.calmMs).toBe(2000)
    expect(ASKS.keysMs).toBe(5000)
  })

  it('counts a held pointer, keys in the last five seconds and the wheel in the last two as busy', () => {
    const rest = { now: 60_000, held: false, lastKeyAt: 0, lastWheelAt: 0 }
    expect(isBusy(rest)).toBe(false)
    expect(isBusy({ ...rest, held: true })).toBe(true)
    expect(isBusy({ ...rest, lastKeyAt: rest.now - 4999 })).toBe(true)
    expect(isBusy({ ...rest, lastKeyAt: rest.now - 5000 })).toBe(false)
    // A person who scrolls his page, a notch every 0.4 s, is busy all the while, and for two seconds after the last notch.
    expect(isBusy({ ...rest, lastWheelAt: rest.now - 400 })).toBe(true)
    expect(isBusy({ ...rest, lastWheelAt: rest.now - 1999 })).toBe(true)
    expect(isBusy({ ...rest, lastWheelAt: rest.now - 2000 })).toBe(false)
    expect(ASKS.wheelMs).toBe(2000)
  })

  it('gives an ask\'s panel no notch of the wheel before its first picture and in its first second, and every other panel all of them', () => {
    const t = 50_000
    expect(wheelFate({ now: t, pictureAt: null, ask: true })).toBe('hold')
    expect(wheelFate({ now: t + 999, pictureAt: t, ask: true })).toBe('hold')
    expect(wheelFate({ now: t + 1000, pictureAt: t, ask: true })).toBe('send')
    // A page that the person opened scrolls as it did, also while its picture is on its way.
    expect(wheelFate({ now: t, pictureAt: null, ask: false })).toBe('send')
    expect(wheelFate({ now: t + 10, pictureAt: t, ask: false })).toBe('send')
  })

  it('leaves room under the panel for the phone\'s own field when the person types in a page', () => {
    const free = askPanelFlat({ width: 390, height: 754, fovDeg: 50 }, M_PER_PX)
    const typing = askPanelFlat({ width: 390, height: 754, fovDeg: 50 }, M_PER_PX, 44, 56)
    expect(free.css[1]).toBe(698)
    expect(typing.css[1]).toBe(698 - 56)
    expect(typing.scale).toBe(free.scale)
    expect(typing.css[1] * typing.scale + 44 + 56).toBeLessThanOrEqual(754)
  })

  it('brings up one at a time: the next waits while an ask\'s panel stands in view', () => {
    const book = waiting()
    settle(book, 'mira')
    expect(nextStep(book, { ...calm, panels: [{ bot: 'mira', inView: true, partInView: true }] })).toEqual({ do: 'nothing' })
    // The person turned away from it: the next comes up, on its own panel.
    expect(nextStep(book, { ...calm, panels: [{ bot: 'mira', inView: false, partInView: true }] })).toEqual({ do: 'up', bot: 'juno' })
    expect(nextStep(book, { ...calm, panels: [{ bot: 'mira', inView: false, partInView: false }] })).toEqual({ do: 'up', bot: 'juno' })
  })

  it('opens no second ask\'s panel for a Bot: while a part of it is in the picture nothing moves, and only out of the picture it comes up again', () => {
    const book = waiting()
    settle(book, 'juno')
    expect(nextStep(book, { ...calm, panels: [{ bot: 'mira', inView: true, partInView: true }] })).toEqual({ do: 'nothing' })
    expect([...book.waiting]).toEqual([])
    // Its middle is out of the picture, a part of it is in: a press may be on its way to its buttons. Nothing moves.
    const part = waiting()
    settle(part, 'juno')
    expect(nextStep(part, { ...calm, panels: [{ bot: 'mira', inView: false, partInView: true }] })).toEqual({ do: 'nothing' })
    expect([...part.waiting]).toEqual([])
    const again = waiting()
    settle(again, 'juno')
    expect(nextStep(again, { ...calm, panels: [{ bot: 'mira', inView: false, partInView: false }] })).toEqual({ do: 'again', bot: 'mira' })
    // That waits for a calm moment too.
    expect(nextStep(again, { ...calm, calmSince: null, panels: [{ bot: 'mira', inView: false, partInView: false }] })).toEqual({ do: 'nothing' })
  })

  it('says that the room is full when twelve pages are open, and comes up when one closed', () => {
    const book = waiting()
    expect(nextStep(book, { ...calm, open: 12 })).toEqual({ do: 'full', bot: 'mira', ask: 'm1' })
    expect(nextStep(book, { ...calm, open: 11 })).toEqual({ do: 'up', bot: 'mira' })
  })

  it('brings nothing up after an open was refused or a page failed, until that is lifted', () => {
    expect(nextStep(waiting(), { ...calm, stopped: true })).toEqual({ do: 'nothing' })
  })
})

describe('what becomes of a press', () => {
  const plain: PressFacts = { now: 20_000, pictureAt: 1000, ask: false, done: false, botPage: false, botAsks: false, pressAt: null, changedAt: null, closedAt: null }
  const asking: PressFacts = { ...plain, ask: true, botPage: true, botAsks: true }

  it('sends a press on a page that has its picture', () => {
    expect(pressFate(plain)).toBe('send')
    expect(pressFate({ ...plain, botPage: true })).toBe('send')
  })

  it('holds a press back before the first picture, on every panel, and on a panel that came back into view until its new picture', () => {
    expect(pressFate({ ...plain, pictureAt: null })).toBe('hold')
    expect(pressFate({ ...asking, pictureAt: null, done: true })).toBe('hold')
  })

  it('holds a press on an ask\'s panel back in the second after its first picture', () => {
    for (const [after, fate] of [[0, 'hold'], [300, 'hold'], [ASKS.firstPictureMs - 1, 'hold'], [ASKS.firstPictureMs, 'send'], [1500, 'send']] as const) {
      expect(pressFate({ ...asking, pictureAt: asking.now - after }), `${after} ms`).toBe(fate)
    }
    // A panel that the person opened has no such second.
    expect(pressFate({ ...asking, ask: false, pictureAt: asking.now - 300 })).toBe('send')
    expect(ASKS.firstPictureMs).toBe(1000)
  })

  it('holds a second press on a Bot\'s page back for half a second while that Bot asks, on an ask\'s panel and on one that the person opened', () => {
    for (const ask of [true, false]) {
      expect(pressFate({ ...asking, ask, pressAt: asking.now - 200 })).toBe('hold')
      expect(pressFate({ ...asking, ask, pressAt: asking.now - ASKS.afterPressMs })).toBe('send')
    }
    // With no ask there is no card that could move under the finger.
    expect(pressFate({ ...plain, botPage: true, pressAt: plain.now - 200 })).toBe('send')
    expect(ASKS.afterPressMs).toBe(500)
  })

  it('holds a press back for a second after that Bot\'s asks changed', () => {
    expect(pressFate({ ...asking, changedAt: asking.now - 999 })).toBe('hold')
    expect(pressFate({ ...asking, changedAt: asking.now - ASKS.afterChangeMs })).toBe('send')
    // The last ask went: the page of a Bot that the person opened waits its second too.
    expect(pressFate({ ...plain, botPage: true, changedAt: plain.now - 400 })).toBe('hold')
    expect(pressFate({ ...plain, changedAt: plain.now - 400 })).toBe('send')
  })

  it('lets a press only keep an ask\'s panel once nothing is asked any more, and sends it again after a new ask\'s second', () => {
    const done: PressFacts = { ...asking, done: true, botAsks: false, changedAt: asking.now - 1200 }
    expect(pressFate(done)).toBe('keep')
    // A new ask of that Bot ends the three seconds: the panel asks again, and the change holds its second.
    const again: PressFacts = { ...asking, done: false, changedAt: asking.now - 300 }
    expect(pressFate(again)).toBe('hold')
    expect(pressFate({ ...again, changedAt: asking.now - 1000 })).toBe('send')
  })

  it('holds every press back for half a second after an ask\'s panel closed by itself', () => {
    expect(pressFate({ ...plain, closedAt: plain.now - 100 })).toBe('hold')
    expect(pressFate({ ...plain, closedAt: plain.now - ASKS.afterCloseMs })).toBe('send')
  })

  it('holds a press on an ask\'s panel back in the second after the controls that answer an ask changed, and until the page has said where they are', () => {
    // The card's first button comes 1.3 to 2.4 s after the first picture, where the message box stood: the second of the first picture has run out.
    const late: PressFacts = { ...asking, pictureAt: asking.now - 2500 }
    expect(pressFate({ ...late, answersAt: null })).toBe('hold')
    expect(pressFate({ ...late, answersAt: late.now - 10 })).toBe('hold')
    expect(pressFate({ ...late, answersAt: late.now - ASKS.afterAnswersMs + 1 })).toBe('hold')
    expect(pressFate({ ...late, answersAt: late.now - ASKS.afterAnswersMs })).toBe('send')
    expect(ASKS.afterAnswersMs).toBe(1000)
    // A page that the person opened, and a panel whose source cannot say, wait for nothing.
    expect(pressFate({ ...late, ask: false, answersAt: late.now - 10 })).toBe('send')
    expect(pressFate(late)).toBe('send')
    // Once nothing is asked any more, a press only keeps the panel, whatever the controls did.
    expect(pressFate({ ...late, done: true, botAsks: false, answersAt: late.now - 10 })).toBe('keep')
  })

  it('counts that second from when the page\'s marked controls changed in number or in place, and not from its text', () => {
    const book: AnswerBook = new Map()
    const box = '[[14,649,362,40]]'
    const card = '[[30,649,96,29],[132,649,78,29],[216,649,85,29]]'
    // The first word of a page is a change: the gate has not seen these controls before.
    expect(takeAnswers(book, 'ask-1', box, 1000)).toBe(true)
    expect(book.get('ask-1')).toEqual({ places: box, at: 1000 })
    // The same places again are no change, however often the page is asked and however its text grew meanwhile.
    for (const at of [1300, 1600, 1900]) expect(takeAnswers(book, 'ask-1', box, at)).toBe(false)
    expect(book.get('ask-1')?.at).toBe(1000)
    // The card comes: other controls, another count.
    expect(takeAnswers(book, 'ask-1', card, 2400)).toBe(true)
    expect(book.get('ask-1')?.at).toBe(2400)
    // The same count at another place (the card moved) is a change too.
    expect(takeAnswers(book, 'ask-1', card.replace(/649/g, '601'), 3000)).toBe(true)
    // A page that could not be asked has said nothing: what it said before stands.
    expect(takeAnswers(book, 'ask-1', null, 3300)).toBe(false)
    expect(book.get('ask-1')?.at).toBe(3000)
    // Each panel has its own.
    expect(takeAnswers(book, 'ask-2', card, 3400)).toBe(true)
    expect(book.get('ask-1')?.at).toBe(3000)
  })

  it('lets a tap on Allow once through while the page\'s text grows, once the card has stood for a second', () => {
    const book: AnswerBook = new Map()
    const card = '[[30,649,96,29],[132,649,78,29],[216,649,85,29]]'
    takeAnswers(book, 'ask-1', card, 5000)
    // The Bot writes: every answer of the page has more words and the same marked controls at the same places.
    for (const at of [5300, 5600, 5900, 6200]) expect(takeAnswers(book, 'ask-1', card, at)).toBe(false)
    expect(pressFate({ ...asking, now: 6300, pictureAt: 1000, answersAt: book.get('ask-1')?.at ?? null })).toBe('send')
    // It goes at once, and carries the places the gate last heard: the gateway finds the same, and sends it.
    expect(pressPlaces(book.get('ask-1')?.places ?? null)).toEqual([[30, 649, 96, 29], [132, 649, 78, 29], [216, 649, 85, 29]])
  })

  it('has a press carry the places the page last said, and nothing when it said none or something that is no list of places', () => {
    expect(pressPlaces(null)).toBeUndefined()
    expect(pressPlaces('[]')).toEqual([])
    expect(pressPlaces('[[1,2,3,4]]')).toEqual([[1, 2, 3, 4]])
    for (const odd of ['{"a":1}', '[[1,2,3]]', '[["1",2,3,4]]', 'not json', JSON.stringify(Array.from({ length: 41 }, () => [0, 0, 1, 1]))]) expect(pressPlaces(odd)).toBeUndefined()
  })

  it('starts the second anew from the places the gateway said when it did not send a press', () => {
    const book: AnswerBook = new Map()
    const box = '[[14,649,362,40]]'
    takeAnswers(book, 'ask-1', box, 1000)
    expect(pressFate({ ...asking, now: 2600, pictureAt: 1000, answersAt: book.get('ask-1')?.at ?? null })).toBe('send')
    // The card came in the round trip: the gateway's answer has its places, and the next press waits a second from there.
    expect(takeAnswers(book, 'ask-1', '[[30,649,96,29],[132,649,78,29]]', 2610)).toBe(true)
    expect(pressFate({ ...asking, now: 3000, pictureAt: 1000, answersAt: book.get('ask-1')?.at ?? null })).toBe('hold')
    expect(pressFate({ ...asking, now: 2610 + ASKS.afterAnswersMs, pictureAt: 1000, answersAt: book.get('ask-1')?.at ?? null })).toBe('send')
  })

  it('has a tap on a Bot whose ask cannot come up open the Bot\'s own page, as a tap on any Bot does', () => {
    const facts = { open: true, cards: 1, standing: null, partInView: false }
    let asked = 0
    expect(tapAnswer(facts, () => { asked += 1; return true })).toBe(true)
    // A page in the way, or a full room: the panel did not come up, and the tap goes on to the Bot's page.
    expect(tapAnswer(facts, () => { asked += 1; return false })).toBe(false)
    expect(asked).toBe(2)
    // A part of its ask's panel shows: the picture turns to it.
    expect(tapAnswer({ ...facts, standing: 'ask-3', partInView: true }, () => true)).toBe('ask-3')
    // Nothing asked, or the panels are off: a plain tap, and nothing comes up.
    expect(tapAnswer({ ...facts, cards: 0 }, () => { asked += 1; return true })).toBe(false)
    expect(tapAnswer({ ...facts, open: false }, () => { asked += 1; return true })).toBe(false)
    expect(asked).toBe(2)
  })

  it('makes an ask\'s panel the person\'s page by a key at any time, and by a press or a wheel once nothing is asked', () => {
    expect(keepsPanel('key', false)).toBe(true)
    expect(keepsPanel('key', true)).toBe(true)
    expect(keepsPanel('press', true)).toBe(true)
    expect(keepsPanel('wheel', true)).toBe(true)
    // While the ask waits, a wheel is reading and a press is the answer.
    expect(keepsPanel('wheel', false)).toBe(false)
    expect(keepsPanel('press', false)).toBe(false)
  })
})

describe('the lens that an ask set back', () => {
  const gone = { panels: 0, coming: false }

  it('goes back to where it was once the ask\'s panel is gone', () => {
    // The person was 1.6 times nearer; the ask set the lens to the start's.
    const kept = lensSetBack(null, 1.6, 1)
    expect(kept).toEqual({ from: 1.6, to: 1 })
    expect(lensGoesBack(kept, { near: 1, panels: 1, coming: false })).toBeNull()
    expect(lensGoesBack(kept, { near: 1, ...gone })).toBe(1.6)
  })

  it('is not kept when the ask did not move the lens', () => {
    expect(lensSetBack(null, 1, 1)).toBeNull()
    expect(lensSetBack(null, 0.8, 0.8)).toBeNull()
    expect(lensGoesBack(null, { near: 1, ...gone })).toBeNull()
  })

  it('goes back once when asks follow one another: after the last panel, to where it was before the first', () => {
    const first = lensSetBack(null, 1.6, 1)
    // The second ask comes up with the lens at the start's already: what is kept is the first's.
    const second = lensSetBack(first, 1, 1)
    expect(second).toEqual({ from: 1.6, to: 1 })
    // The first panel is gone and the second is about to come up, or stands: not yet.
    expect(lensGoesBack(second, { near: 1, panels: 0, coming: true })).toBeNull()
    expect(lensGoesBack(second, { near: 1, panels: 1, coming: false })).toBeNull()
    expect(lensGoesBack(second, { near: 1, ...gone })).toBe(1.6)
  })

  it('starts the quiet time when an ask\'s coming up moved the lens, and only then', () => {
    // The eye stood 1.3 times nearer: every page is drawn smaller at once, and a press on its way lands beside its aim.
    expect(lensMoved(1.3, 1)).toBe(true)
    // At the start's lens, or with the eye stepped back, the ask moves no lens: nothing is held back for it.
    expect(lensMoved(1, 1)).toBe(false)
    expect(lensMoved(0.8, 0.8)).toBe(false)
    // What is kept for the way back is kept exactly when the lens moved.
    expect(lensSetBack(null, 1.3, 1) !== null).toBe(lensMoved(1.3, 1))
    expect(lensSetBack(null, 1, 1) !== null).toBe(lensMoved(1, 1))
  })

  it('stays where the person put it when he changed it meanwhile', () => {
    const kept = lensSetBack(null, 1.6, 1)
    expect(lensGoesBack(kept, { near: 1.25, ...gone })).toBeNull()
    expect(lensGoesBack(kept, { near: 0.8, ...gone })).toBeNull()
  })
})

describe('where an ask\'s panel stands', () => {
  const start = (width: number, height: number) => ({ width, height, fovDeg: 50 })

  it('stands on an upright phone where one page px is one screen px, nearer than the first page and the crew', () => {
    const at = askPanelFlat(start(390, 754), M_PER_PX)
    expect(at.css[0]).toBe(390)
    expect(at.css[1]).toBe(698)
    // The page is 12 px narrower than the picture needs: a hair under one to one.
    expect(at.scale).toBeCloseTo(378 / 390, 6)
    expect(at.distanceM).toBeGreaterThan(1.2)
    expect(at.distanceM).toBeLessThan(1.3)
    // The smallest button of an approval's card, 78 by 29 px on the page, is over 40 by 28 px on the screen.
    expect(78 * at.scale).toBeGreaterThan(40)
    expect(29 * at.scale).toBeGreaterThan(28)
    // The panel and the close mark over it fit the picture's height.
    expect(at.css[1] * at.scale + 44).toBeLessThanOrEqual(754)
  })

  it('stands on a desk at one to one, with the whole panel in the picture', () => {
    const at = askPanelFlat(start(1440, 804), M_PER_PX)
    expect(at.scale).toBe(1)
    expect(at.css).toEqual([390, 700])
    expect(at.distanceM).toBeGreaterThan(PAGES.nearM)
    expect(at.distanceM).toBeLessThan(1.45)
  })

  it('stands farther on a phone on its side, where the picture is too low for the panel at one to one', () => {
    const at = askPanelFlat(start(844, 340), M_PER_PX)
    expect(at.css).toEqual([390, 480])
    expect(at.scale).toBeCloseTo(284 / 480, 6)
    // What the picture's height allows: a button 78 by 29 px is 46 by 17 px there.
    expect(Math.round(78 * at.scale)).toBe(46)
    expect(Math.round(29 * at.scale)).toBe(17)
  })

  it('comes nearer when the eye has stepped back, so that the buttons keep their size on the screen', () => {
    const wide = askPanelFlat({ width: 390, height: 754, fovDeg: 64 }, M_PER_PX)
    expect(wide.scale).toBeCloseTo(378 / 390, 6)
    expect(wide.distanceM).toBeLessThan(askPanelFlat(start(390, 754), M_PER_PX).distanceM)
    expect(wide.distanceM).toBeGreaterThan(PAGES.nearM)
  })

  it('stands in a session where a hand\'s ray holds its buttons', () => {
    const deg = (px: number) => (Math.atan((px * M_PER_PX) / ASK_SESSION.distanceM) * 180) / Math.PI
    expect(deg(29)).toBeGreaterThan(PAGES.edgeDeg)
    expect(deg(78)).toBeGreaterThan(3.4)
    // The whole panel is inside the glasses' 40.7 degrees, up and down.
    expect(deg(ASK_SESSION.css[1] / 2) * 2).toBeLessThan(38)
  })
})

describe('nearer than everything that stands there', () => {
  const eye = [0, 1.6, 0] as const
  /** A panel 0.59 m wide and 1.06 m tall, wanted 1.27 m away, straight ahead and level. */
  const ask: AskPlaceAsk = { eye, ahead: [0, 0, -1], widthM: 390 * M_PER_PX, heightM: 698 * M_PER_PX, distanceM: 1.27, leastM: PAGES.nearM, things: [] }
  /** A page of the size it opens at, facing the eye, so far away and so far to the right. */
  const page = (distanceM: number, rightM = 0) => facePoints([rightM, 1.6, -distanceM], [(PAGES.cssWidth * M_PER_PX) / 2, 0, 0], [0, (PAGES.cssHeight * M_PER_PX) / 2, 0])

  it('stands straight ahead at the distance it asks for when everything there is farther', () => {
    const got = askPlace({ ...ask, things: page(PAGES.distanceM) })
    expect(got).toMatchObject({ distanceM: 1.27, raisedM: 0 })
    expect(got?.at[0]).toBeCloseTo(0, 9)
    expect(got?.at[1]).toBeCloseTo(1.6, 9)
    expect(got?.at[2]).toBeCloseTo(-1.27, 9)
  })

  it('comes 5 cm nearer than a page that a hand pulled nearer than that', () => {
    const got = askPlace({ ...ask, things: [...page(PAGES.distanceM), ...page(1.1)] })
    expect(got?.distanceM).toBeCloseTo(1.1 - ASK_CLEAR_M, 6)
    expect(ASK_CLEAR_M).toBe(0.05)
  })

  it('counts its whole face: a page that only its edge would cover is come nearer than, and one beside its face is not', () => {
    // The near page's left edge is 0.2 m right of the middle: seen through the panel's plane that is 0.25 m, inside its half width of 0.296 m.
    const edge = askPlace({ ...ask, things: page(1.0, 0.2 + (PAGES.cssWidth * M_PER_PX) / 2) })
    expect(edge?.distanceM).toBeCloseTo(1.0 - ASK_CLEAR_M, 6)
    // 0.5 m to the right it is beside the face, also once the panel is as near as it.
    expect(askPlace({ ...ask, things: page(1.0, 0.5 + (PAGES.cssWidth * M_PER_PX) / 2) })?.distanceM).toBe(1.27)
  })

  it('looks again from nearer: a nearer panel covers more of the view', () => {
    // At 1.27 m the face does not cover the page at the side; once a page ahead has brought it to 0.75 m, it does.
    const side = page(0.7, 0.2 + (PAGES.cssWidth * M_PER_PX) / 2)
    expect(askPlace({ ...ask, things: side })?.distanceM).toBe(1.27)
    const got = askPlace({ ...ask, things: [...page(0.8), ...side] })
    expect(got?.distanceM).toBeCloseTo(0.7 - ASK_CLEAR_M, 6)
  })

  it('does not come up when that is nearer than a page may stand, or than its buttons are whole in the picture', () => {
    expect(askPlace({ ...ask, things: page(PAGES.nearM + 0.04) })).toBeNull()
    // On a screen the least is where the panel still fits the picture: a page 1.2 m away leaves it no such place.
    expect(askPlace({ ...ask, leastM: 1.2, things: page(1.2) })).toBeNull()
    expect(askPlace({ ...ask, leastM: 1.2, things: page(1.26) })?.distanceM).toBeCloseTo(1.21, 6)
  })

  it('is raised clear of the desk of the drawn room, and stands no nearer for it', () => {
    // The head 25 degrees down at the crew, in a session: the panel 1.4 m away would cross the desk's near rim.
    const down = (25 * Math.PI) / 180
    const session: AskPlaceAsk = { ...ask, ahead: [0, -Math.sin(down), -Math.cos(down)], widthM: 390 * M_PER_PX, heightM: 560 * M_PER_PX, distanceM: 1.4 }
    const desk: [number, number, number][] = []
    for (let yaw = -52; yaw <= 52; yaw += 2) for (const r of [1.12, 1.3, 1.5, 1.78]) for (const y of [0.72, 0.68]) desk.push([Math.sin((yaw * Math.PI) / 180) * r, y, -Math.cos((yaw * Math.PI) / 180) * r])
    const free = askPlace(session)
    expect(free).toMatchObject({ distanceM: 1.4, raisedM: 0 })
    const got = askPlace({ ...session, desk })
    expect(got?.distanceM).toBe(1.4)
    expect(got?.raisedM).toBeGreaterThan(0.02)
    expect(got?.raisedM).toBeLessThan(0.3)
    // Raised, no point of the desk that is nearer than the panel is seen through its face.
    expect(askPlace({ ...session, desk, eye: [0, 1.6 + 0, 0] })?.raisedM).toBe(got?.raisedM)
    // Level and straight ahead the desk is far under the panel: nothing is raised.
    expect(askPlace({ ...ask, desk })).toMatchObject({ distanceM: 1.27, raisedM: 0 })
  })

  it('comes up on an upright phone with a crew whose fronts stand 1.20 m away: one centimetre nearer, with the buttons whole', () => {
    const view = { width: 390, height: 754, fovDeg: 50 }
    const size = askPanelFlat(view, M_PER_PX)
    const [widthM, heightM] = [size.css[0] * M_PER_PX, size.css[1] * M_PER_PX]
    // The crew stands under the eye's line, where the panel's lower part covers it: three fronts side by side.
    const fronts = [-0.23, 0, 0.23].flatMap(x => facePoints([x, 1.6 - 0.36, -1.2], [0.108, 0, 0], [0, 0.16, 0], 2, 4))
    // On the screen the panel's middle is half the close mark under the picture's middle; its lower end is held.
    const mPerScreenPx = (size.distanceM * 2 * Math.tan((25 * Math.PI) / 180)) / view.height
    const middleUp = -(22 * mPerScreenPx) / size.distanceM
    const flat: AskPlaceAsk = { eye, ahead: [0, 0, -1], low: middleUp - heightM / (2 * size.distanceM), widthM, heightM, distanceM: size.distanceM, leastM: askLeastFlat(view, size, PAGES.nearM), things: page(PAGES.distanceM), bots: fronts }
    const got = askPlace(flat)
    expect(ASK_CLEAR_BOT_M).toBe(0.01)
    expect(got?.distanceM).toBeCloseTo(1.2 - ASK_CLEAR_BOT_M, 6)
    // So near, a page px is more than a screen px: the page is wider than the picture by under 30 page px a side.
    const scale = (size.scale * size.distanceM) / got!.distanceM
    const cutPagePx = (size.css[0] * scale - view.width) / 2 / scale
    expect(cutPagePx).toBeGreaterThan(0)
    expect(cutPagePx).toBeLessThan(30)
    expect(ASK_SIDE_CUT_PX).toBeLessThan(30)
    // The page's lower end is where it was in the picture: so many screen px under the picture's middle.
    const lowerEndPx = (d: number, at: readonly [number, number, number]) => ((1.6 - (at[1] - heightM / 2)) / d) * (view.height / (2 * Math.tan((25 * Math.PI) / 180)))
    const free = askPlace({ ...flat, bots: [] })!
    expect(free.distanceM).toBe(size.distanceM)
    expect(lowerEndPx(got!.distanceM, got!.at)).toBeCloseTo(lowerEndPx(free.distanceM, free.at), 6)
    expect(view.height / 2 + lowerEndPx(got!.distanceM, got!.at)).toBeLessThanOrEqual(view.height)
    // Its smallest button, 78 by 29 px on the page, is larger than at the distance the measure asks for.
    expect(29 * scale).toBeGreaterThan(29 * size.scale)
  })

  it('grows about its middle on a desk when a crew makes it come nearer, so that the close mark keeps its room as long as the picture allows', () => {
    const view = { width: 1440, height: 858, fovDeg: 50 }
    const size = askPanelFlat(view, M_PER_PX)
    const [widthM, heightM] = [size.css[0] * M_PER_PX, size.css[1] * M_PER_PX]
    const t = Math.tan((25 * Math.PI) / 180)
    // How far up for each metre ahead a row of the picture is, `y` px from its upper edge.
    const rise = (y: number) => (1 - (2 * y) / view.height) * t
    const middleUp = rise(view.height / 2 + 22)
    const ends: [number, number] = [rise(view.height - 6), rise(6 + 44)]
    const fronts = [-0.23, 0, 0.23].flatMap(x => facePoints([x, 1.6 - 0.36, -1.3], [0.108, 0, 0], [0, 0.16, 0], 2, 4))
    const flat: AskPlaceAsk = { eye, ahead: [0, 0, -1], low: middleUp - heightM / (2 * size.distanceM), ends, widthM, heightM, distanceM: size.distanceM, leastM: askLeastFlat(view, size, PAGES.nearM), things: page(PAGES.distanceM), bots: fronts }
    const got = askPlace(flat)!
    expect(got.distanceM).toBeLessThan(size.distanceM)
    // Its upper edge stays under the mark's room, and its lower end over the picture's lower end.
    const up = (got.at[1] - 1.6 + heightM / 2) / got.distanceM
    const low = (got.at[1] - 1.6 - heightM / 2) / got.distanceM
    expect(up).toBeLessThanOrEqual(ends[1] + 1e-9)
    expect(low).toBeGreaterThanOrEqual(ends[0] - 1e-9)
    // Held only at its lower end, as before, the mark's room was cut.
    const held = askPlace({ ...flat, ends: undefined })!
    expect((held.at[1] - 1.6 + heightM / 2) / held.distanceM).toBeGreaterThan(ends[1])
  })

  it('stands 5 cm nearer than a page and 1 cm nearer than a Bot, whichever asks for more', () => {
    const bots = facePoints([0, 1.6, -1.1], [0.108, 0, 0], [0, 0.16, 0], 2, 4)
    expect(askPlace({ ...ask, things: page(1.12), bots })?.distanceM).toBeCloseTo(1.12 - ASK_CLEAR_M, 6)
    expect(askPlace({ ...ask, things: page(1.2), bots })?.distanceM).toBeCloseTo(1.1 - ASK_CLEAR_BOT_M, 6)
  })

  it('does not come up on a phone when a page stands so near that a button would leave the picture', () => {
    const view = { width: 390, height: 754, fovDeg: 50 }
    const size = askPanelFlat(view, M_PER_PX)
    const least = askLeastFlat(view, size, PAGES.nearM)
    // Each side of the page may leave the picture by 29 page px: the page drawn 390 / 332 times as large as at one to one.
    expect(least).toBeCloseTo((size.distanceM * size.scale * 332) / 390, 6)
    expect(least).toBeGreaterThan(1.0)
    expect(least).toBeLessThan(1.1)
    expect(askPlace({ ...ask, distanceM: size.distanceM, leastM: least, things: page(least + ASK_CLEAR_M - 0.01) })).toBeNull()
    expect(askPlace({ ...ask, distanceM: size.distanceM, leastM: least, things: page(least + ASK_CLEAR_M + 0.01) })?.distanceM).toBeCloseTo(least + 0.01, 6)
    // A wide picture is no bound: there the least is how near a hand can push a page.
    expect(askLeastFlat({ width: 1440 }, askPanelFlat({ width: 1440, height: 804, fovDeg: 50 }, M_PER_PX), PAGES.nearM)).toBe(PAGES.nearM)
  })
})

describe('the press gates read a page and the panel holds (D55 1, small 3)', () => {
  it('no panel takes a press before its first picture: a page that says it waits gives no picture time', () => {
    const plain: PressFacts = { now: 10_000, pictureAt: 0, ask: false, done: false, botPage: false, botAsks: false, pressAt: null, changedAt: null, closedAt: null }
    const waits = { notYet: () => true, pictureAt: () => null }
    expect(pictureFor(waits)).toBeNull()
    expect(pressFate({ ...plain, pictureAt: pictureFor(waits) })).toBe('hold')
    // Once it takes input: its picture time, or 0 (long ago) when its wait ended with none since it came back.
    expect(pictureFor({ notYet: () => false, pictureAt: () => 1234 })).toBe(1234)
    expect(pictureFor({ notYet: () => false, pictureAt: () => null })).toBe(0)
    expect(pressFate({ ...plain, pictureAt: pictureFor({ notYet: () => false, pictureAt: () => null }) })).toBe('send')
    // A source with no word of its own (a page drawn by the tab) is read by its picture time alone.
    expect(pictureFor({ pictureAt: () => null })).toBeNull()
    expect(pictureFor({})).toBe(0)
  })

  it('holds a press for a page being handed, in its calm time, in the quiet time, or by the ask gate, and else not', () => {
    const w = { handing: new Map<string, string>(), calmUntil: new Map<string, number>(), quiet: () => false, asks: null as ((id: string) => boolean) | null }
    expect(pressHeldNow('a', 100, w)).toBe(false)
    expect(pressHeldNow('a', 100, { ...w, handing: new Map([['a', 'handing']]) })).toBe(true)
    expect(pressHeldNow('a', 100, { ...w, handing: new Map([['a', 'done']]) })).toBe(false)
    expect(pressHeldNow('a', 100, { ...w, calmUntil: new Map([['a', 101]]) })).toBe(true)
    expect(pressHeldNow('a', 101, { ...w, calmUntil: new Map([['a', 101]]) })).toBe(false)
    expect(pressHeldNow('a', 100, { ...w, quiet: now => now < 200 })).toBe(true)
    expect(pressHeldNow('a', 100, { ...w, asks: id => id === 'a' })).toBe(true)
    expect(pressHeldNow('b', 100, { ...w, asks: id => id === 'a' })).toBe(false)
  })
})
