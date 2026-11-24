import { describe, expect, it } from 'vitest'
import { HANDS, WAYS, answerOf, handBack, onCardForWord, pointAndSay, whereLet, type AskMoment } from './approval'
import { choicesFor, fan, rowsOf, type Choice } from './choices'
import { Dwell } from './gestures'
import { takerOf } from './handling'
import { CHOICE, degOf, pxFor } from './metrics'
import { CHOICE_PX, choiceRects } from './panels'
import { TUNE } from './tune'

const W = { take: 'Take this over', unblock: 'Find out what blocks this', status: 'Give me a short status of this', summarise: 'Summarise this for me', more: 'More' }
const SAY = { yesWords: 'yes|yeah|ok', noWords: 'no|nope', waitWords: 'wait' }
const moment = (m: Partial<AskMoment>): AskMoment => ({ onCard: false, words: '', letGo: '', ...m })
/** `n` cards that are not "more", for the layout rules. */
const many = (n: number): Choice[] => Array.from({ length: n }, (_, i) => ({ id: 'status', label: `card ${i + 1}` }))
/** The canvas the cards are drawn on. */
const CANVAS = { w: CHOICE.px, h: 480 }

describe('H1: with hands only, yes is the card put back in the Bot\'s hand', () => {
  it('uses the hand-back alone: the spoken yes is parked, not gone', () => {
    expect(HANDS).toEqual([handBack])
    expect(WAYS).toContain(pointAndSay)
  })

  it('allows a card that ends in the Bot\'s hand, and nothing else', () => {
    const inHand = { bin: false, botM: 0.02, reachM: TUNE.handBackM, speedMps: 0.05 }
    expect(answerOf(moment({ onCard: true, letGo: whereLet('place', inHand) }), SAY, HANDS)).toBe('yes')
    // A bump toward the Bot, and a card that is only held.
    expect(answerOf(moment({ onCard: true, letGo: whereLet('place', { ...inHand, botM: 0.08 }) }), SAY, HANDS)).toBe('nothing')
    expect(answerOf(moment({ onCard: true }), SAY, HANDS)).toBe('nothing')
  })

  it('does not hear a word: a finger on the card and "ok" answers nothing', () => {
    const point = onCardForWord({ wordAt: 10_000, restAt: 9_000, onSince: 9_800, onUntil: 10_400, fastest: 0.05 })
    expect(point).toBe(true)
    expect(answerOf(moment({ onCard: point, words: 'Ok.' }), SAY, HANDS)).toBe('nothing')
    expect(answerOf(moment({ onCard: point, words: 'Ok.' }), SAY, WAYS)).toBe('yes')
  })

  it('still refuses by the bin', () => {
    expect(answerOf(moment({ onCard: true, letGo: 'bin' }), SAY, HANDS)).toBe('no')
  })
})

describe('H2\': choice cards never overlap', () => {
  it('keeps a gap between any two cards, for every count a Bot can hold out', () => {
    for (let n = 1; n <= TUNE.choiceMax; n += 1) {
      const rects = choiceRects(rowsOf(n), CANVAS.w, CANVAS.h)
      expect(rects).toHaveLength(n)
      for (const a of rects) {
        // Every card is whole: inside the canvas.
        expect(a.x).toBeGreaterThanOrEqual(0)
        expect(a.x + a.w).toBeLessThanOrEqual(CANVAS.w)
        expect(a.y).toBeGreaterThanOrEqual(0)
        expect(a.y + a.h).toBeLessThanOrEqual(CANVAS.h)
        for (const b of rects) {
          if (a === b) continue
          const apartX = a.x + a.w + CHOICE_PX.gap <= b.x || b.x + b.w + CHOICE_PX.gap <= a.x
          const apartY = a.y + a.h + CHOICE_PX.gap <= b.y || b.y + b.h + CHOICE_PX.gap <= a.y
          expect(apartX || apartY).toBe(true)
        }
      }
    }
  })
})

describe('H3\': two or three in one row, four to eight in rows of up to four, "more" past eight', () => {
  it('stands two or three cards in one row', () => {
    expect(rowsOf(1)).toEqual([1])
    expect(rowsOf(2)).toEqual([2])
    expect(rowsOf(3)).toEqual([3])
  })

  it('stands four to eight in rows of up to four, as even as they can be', () => {
    expect(rowsOf(4)).toEqual([4])
    expect(rowsOf(5)).toEqual([3, 2])
    expect(rowsOf(6)).toEqual([3, 3])
    expect(rowsOf(7)).toEqual([4, 3])
    expect(rowsOf(8)).toEqual([4, 4])
    for (let n = 4; n <= 8; n += 1) expect(Math.max(...rowsOf(n))).toBeLessThanOrEqual(TUNE.choiceRowMax)
  })

  it('shows up to eight cards whole, with no "more"', () => {
    const f = fan(many(8), 0, W)
    expect(f.cards).toHaveLength(8)
    expect(f.cards.some(c => c.id === 'more')).toBe(false)
    expect(f.pages).toBe(1)
  })

  it('past eight, makes the last card "more", and never shows more than eight', () => {
    const all = many(12)
    const first = fan(all, 0, W)
    expect(first.cards).toHaveLength(8)
    expect(first.cards.at(-1)).toEqual({ id: 'more', label: 'More' })
    expect(first.cards.slice(0, 7).map(c => c.label)).toEqual(all.slice(0, 7).map(c => c.label))
    // "More" turns to the cards that were not on show, and then back to the first ones.
    const second = fan(all, 1, W)
    expect(second.cards.slice(0, -1).map(c => c.label)).toEqual(all.slice(7).map(c => c.label))
    expect(second.cards.at(-1)?.id).toBe('more')
    expect(fan(all, 2, W).cards).toEqual(first.cards)
    expect(first.pages).toBe(2)
  })
})

describe('H4: why is a point that rests 0.4 s, never a tap or a passing hand', () => {
  it('asks why once the finger has rested 0.4 s on the thing', () => {
    expect(TUNE.whyHoldMs).toBe(400)
    const d = new Dwell(TUNE.whyHoldMs)
    expect(d.feed(true, 1000)).toBe(false)
    expect(d.feed(true, 1399)).toBe(false)
    expect(d.feed(true, 1400)).toBe(true)
    // It asks once, however long the finger stays.
    expect(d.feed(true, 3000)).toBe(false)
  })

  it('asks nothing of a hand that passes over, or that taps and leaves', () => {
    const d = new Dwell(TUNE.whyHoldMs)
    expect(d.feed(true, 1000)).toBe(false)
    expect(d.feed(true, 1200)).toBe(false)
    expect(d.feed(false, 1250)).toBe(false)
    // Back on the thing: the rest starts again from nothing.
    expect(d.feed(true, 1300)).toBe(false)
    expect(d.feed(true, 1650)).toBe(false)
    expect(d.feed(true, 1700)).toBe(true)
  })
})

describe('H6: work that is thrown goes to the Bot that was lit', () => {
  it('goes to the lit Bot when the hand let go soon after it lit', () => {
    expect(TUNE.throwAimMs).toBe(400)
    expect(takerOf('throw', '', 'juno', 0)).toBe('juno')
    expect(takerOf('throw', '', 'juno', 400)).toBe('juno')
  })

  it('goes to no Bot when none was lit, or one was lit too long ago: then it is the bin', () => {
    expect(takerOf('throw', '', '', 0)).toBe('')
    expect(takerOf('throw', '', 'juno', 401)).toBe('')
    // A throw never goes to a Bot the ray only happens to be on as the hand lets go.
    expect(takerOf('throw', 'atlas', '', 0)).toBe('')
  })

  it('put down, goes to the Bot it is within reach of', () => {
    expect(takerOf('place', 'atlas', 'juno', 0)).toBe('atlas')
    expect(takerOf('place', '', 'juno', 0)).toBe('')
  })
})

describe('H8: a choice card is at least 10.5 by 6.5 cm at 0.6 m, and its title is one degree tall', () => {
  it('is that big at the eye', () => {
    expect([TUNE.choiceCardWM, TUNE.choiceCardHM, TUNE.choiceAtM]).toEqual([0.105, 0.065, 0.6])
    const metres = (px: number): number => (px / CHOICE.px) * CHOICE.width
    // As big at the surface's own distance as the rule's card is at 0.6 m.
    expect(metres(CHOICE_PX.w) / CHOICE.distance).toBeGreaterThanOrEqual(TUNE.choiceCardWM / TUNE.choiceAtM)
    expect(metres(CHOICE_PX.h) / CHOICE.distance).toBeGreaterThanOrEqual(TUNE.choiceCardHM / TUNE.choiceAtM)
  })

  it('writes its title one degree tall', () => {
    expect(degOf(CHOICE, pxFor(CHOICE, 1))).toBeGreaterThanOrEqual(1)
  })

  it('fits a full row of four, and two rows, on its canvas', () => {
    expect(4 * CHOICE_PX.w + 5 * CHOICE_PX.gap).toBeLessThanOrEqual(CANVAS.w)
    expect(2 * (CHOICE_PX.h + CHOICE_PX.gap)).toBeLessThanOrEqual(CANVAS.h)
  })
})

describe('H9: a choice is made by pinching its card; only an approval needs the hand-back', () => {
  it('offers only things a Bot can be told, never an approval', () => {
    for (const state of ['needs', 'failed', 'working', 'idle', '']) {
      for (const owner of ['atlas', 'juno', '']) {
        const ids = choicesFor({ owner, state }, 'atlas', W).map(c => c.id)
        expect(ids.length).toBeGreaterThanOrEqual(2)
        for (const id of ids) expect(['take', 'unblock', 'status', 'summarise']).toContain(id)
      }
    }
  })

  it('offers "take this over" only for work that belongs to another Bot, and "unblock" only for work that waits or failed', () => {
    expect(choicesFor({ owner: 'juno', state: 'needs' }, 'atlas', W).map(c => c.id)).toEqual(['take', 'unblock', 'status', 'summarise'])
    expect(choicesFor({ owner: 'atlas', state: 'needs' }, 'atlas', W).map(c => c.id)).toEqual(['unblock', 'status', 'summarise'])
    expect(choicesFor({ owner: 'atlas', state: 'working' }, 'atlas', W).map(c => c.id)).toEqual(['status', 'summarise'])
    expect(choicesFor({ owner: 'juno', state: 'failed' }, 'atlas', W)[1].id).toBe('unblock')
  })

  it('sends the card\'s own words', () => {
    expect(choicesFor({ owner: 'juno', state: '' }, 'atlas', W)[0]).toEqual({ id: 'take', label: 'Take this over' })
  })
})
