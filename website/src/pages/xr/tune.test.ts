import { describe, expect, it } from 'vitest'
import { KEY } from './gestures'
import { openAmount, throwKind } from './handling'
import { DISARM_MS } from './input'
import { CARD, FULL } from './metrics'
import { THING_ROW_PX } from './panels'
import { POINT } from './pointTalk'
import { DRAG_START } from './sceneBase'
import { MARBLE_R, OFFER_AT, OFFER_SCALE } from './tokens'
import { TUNE } from './tune'

describe('the one table every gesture is read from', () => {
  it('holds the numbers the feel labs and the amendments to the grammar settled on', () => {
    expect(TUNE).toMatchObject({
      holdStartM: 0.08, rowMinM: 0.034, pinchOpenMs: 70, twoHandRule: 'window', gapOpenM: 0.1, gapCloseM: 0.055, zoomGain: 3.6,
      throwFlyMps: 0.7, keyTurnDeg: 68, keyPullM: 0.04, sayPauseMs: 600, sayBeatMs: 900,
      cardRestMs: 600, cardTouchM: 0.025, cardStayMs: 350, cardStillMps: 0.15, handBackM: 0.03, yesBeatMs: 600,
      whyHoldMs: 400, choiceRowMax: 4, choiceMax: 8, choiceCardWM: 0.105, choiceCardHM: 0.065, choiceAtM: 0.6, throwAimMs: 400,
      bothPalmsMs: 350, palmsPushM: 0.06, pullBackM: 0.06, carryScale: 0.4,
    })
  })

  it('is what the gestures really use: no second copy of a number', () => {
    expect(KEY.TURN_DEG).toBe(TUNE.keyTurnDeg)
    expect(KEY.PULL_M).toBe(TUNE.keyPullM)
    expect(POINT.PAUSE_MS).toBe(TUNE.sayPauseMs)
    expect(POINT.PAUSE_OPEN_MS).toBe(TUNE.sayPauseOpenMs)
    expect(POINT.BEAT_MS).toBe(TUNE.sayBeatMs)
    // A hold starts when the hand has moved 8 cm at reading distance, read as an angle.
    expect(Math.tan(DRAG_START) * FULL.distance).toBeCloseTo(TUNE.holdStartM, 9)
    expect(openAmount(TUNE.gapOpenM, 'window')).toBe(1)
    expect(openAmount(TUNE.gapCloseM, 'window')).toBe(0)
    expect(throwKind((TUNE.throwFlyMps * 180) / Math.PI, 1)).toBe('throw')
  })

  it('keeps the numbers in an order that works', () => {
    expect(TUNE.gapCloseM).toBeLessThan(TUNE.gapOpenM)
    // The key turns well before the picture's quarter turn, and far past a shaking hand.
    expect(TUNE.keyTurnDeg).toBeLessThan(90)
    expect(TUNE.keyTurnDeg).toBeGreaterThan(KEY.COMMIT_DEG * 2)
    // A whole sentence ends sooner than an open one, and a send waits long enough to say "never mind".
    expect(TUNE.sayPauseMs).toBeLessThan(TUNE.sayPauseOpenMs)
    expect(TUNE.sayBeatMs).toBeGreaterThanOrEqual(800)
    // Every hand gesture fits a resting hand: no number asks for more than 10 cm.
    for (const m of [TUNE.holdStartM, TUNE.gapOpenM, TUNE.keyPullM]) expect(m).toBeLessThanOrEqual(0.1)
  })

  it('C6: opens a thing between two hands at the fixed 0.10 m window, not at twice the first gap', () => {
    expect(TUNE.twoHandRule).toBe('window')
    expect(openAmount(0.1)).toBe(1)
    expect(openAmount(0.055)).toBe(0)
    // Hands that took hold 3 cm apart and doubled that are not open: only the gap itself counts.
    expect(openAmount(0.06, TUNE.twoHandRule, 0.03, 0)).toBeLessThan(0.5)
    // And hands that took hold wide need no doubling to be open.
    expect(openAmount(0.1, TUNE.twoHandRule, 0.09, 0)).toBe(1)
  })

  it('keeps the card numbers in an order that works', () => {
    // The finger must stay on the card for less time than the beat lasts, or no yes could be said before the beat ends.
    expect(TUNE.cardStayMs).toBeLessThan(TUNE.yesBeatMs)
    // A hand that puts the card down slowly is far under a throw.
    expect(TUNE.cardStillMps).toBeLessThan(TUNE.throwFlyMps)
    // The touch is a fingertip's own depth, and the Bot's hand is no bigger than a pinchable row.
    expect(TUNE.cardTouchM).toBeLessThanOrEqual(0.03)
    expect(TUNE.handBackM).toBeLessThanOrEqual(TUNE.rowMinM)
  })

  it('makes everything a hand must pinch at least as high as a row must be', () => {
    expect((THING_ROW_PX * FULL.width) / FULL.px).toBeGreaterThanOrEqual(TUNE.rowMinM)
    expect(MARBLE_R * 2).toBeGreaterThanOrEqual(TUNE.rowMinM)
  })

  it('draws the card a Bot holds out small because it is close, and never smaller at the eye than a card at reading distance', () => {
    expect((OFFER_SCALE * CARD.width) / OFFER_AT.length()).toBeGreaterThanOrEqual(CARD.width / CARD.distance)
    // It is small enough to stand over the far edge of the land without covering the Bots: under 30 degrees across.
    expect((2 * Math.atan((OFFER_SCALE * CARD.width) / 2 / OFFER_AT.length()) * 180) / Math.PI).toBeLessThan(30)
  })

  it('lets a card be answered only once it has been held out a moment: the guard\'s own wait covers it', () => {
    // A yes or a no to the card is a guarded target. A press that begins sooner than this after it appeared is refused as "disarmed".
    expect(DISARM_MS).toBeGreaterThanOrEqual(600)
  })
})
