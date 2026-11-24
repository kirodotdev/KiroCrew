import { describe, expect, it } from 'vitest'
import { CARD_KEYS_TRIES, cardKeysBox, cardKeysSeen, cardKeysSpot, cardLowDeg, keysLineTopDeg } from './cardKeysPlace'
import { CARD } from './chatCard'
import { KEYS } from './deskKeys'
import { PAGES } from './layout'
import { PLACES } from './places'

const head = { x: 0, y: PAGES.eyeM, z: 0 }
/** A Bot's feet on the floor, so many degrees to the right of a head at `from`, so far away. */
const feet = (yawDeg: number, m = 1.6, from = head): number[] => { const r = (yawDeg * Math.PI) / 180; return [from.x + Math.sin(r) * m, 0, from.z - Math.cos(r) * m] }
const [own, other, far, farOther] = CARD_KEYS_TRIES

describe('where the chat card\'s keys come out', () => {
  it('takes the card\'s own side when no Bot asks or failed', () => {
    expect(cardKeysSpot(head, 0, [])).toEqual(own)
  })
  it('tries the card\'s own height only: its side, the other side, then 36 degrees to each side (M84)', () => {
    // The height is the build's own (`KEYS.standPitchDeg`, -12.5 since M87): read, not typed again.
    const h = KEYS.standPitchDeg
    expect(CARD_KEYS_TRIES).toEqual([[-18, h], [18, h], [-36, h], [36, h]])
    expect(h).toBe(-12.5)
  })
  it('leaves a side where a Bot that asks stands under the keys: the other side, then twice as far', () => {
    expect(cardKeysSpot(head, 0, [feet(own[0], 4)])).toEqual(other)
    // A Bot that asks on each side of his look.
    expect(cardKeysSpot(head, 0, [feet(own[0], 4), feet(other[0], 4)])).toEqual(far)
    expect(cardKeysSpot(head, 0, [feet(own[0], 4), feet(other[0], 4), feet(far[0], 4)])).toEqual(farOther)
  })
  it('with a Bot at every place lies over the one that is farthest from his look', () => {
    // One Bot under each place: 18 left, 18 right, 45 left, 40 right. The one 45 left is the farthest from his look.
    expect(cardKeysSpot(head, 0, [feet(-18, 4), feet(18, 4), feet(-45, 4), feet(40, 4)])).toEqual(far)
    expect(cardKeysSpot(head, 0, [feet(-18, 4), feet(18, 4), feet(-40, 4), feet(45, 4)])).toEqual(farOther)
    // His look turned 30 degrees to the left: the same four Bots, and now the one at his far right is the farthest.
    expect(cardKeysSpot(head, -30, [feet(-48, 4), feet(-12, 4), feet(-70, 4), feet(15, 4)])).toEqual(farOther)
  })
  it('reckons the box from his head, not from the room\'s middle (T9)', () => {
    // He stands two metres to the right of the middle: a Bot 18 degrees left of HIS look lies under the first place.
    const aside = { x: 2, y: PAGES.eyeM, z: 0 }
    expect(cardKeysSpot(aside, 0, [feet(own[0], 4, aside)])).toEqual(other)
    // Seen from the middle the same Bot is elsewhere: reckoned from there the first place would be clear.
    expect(cardKeysSpot(head, 0, [feet(own[0], 4, aside)])).toEqual(own)
  })
  it('tries only places that the lens holds whole while he looks at the card; a place 26 degrees down is cut there (D109 3)', () => {
    for (const [side, down] of CARD_KEYS_TRIES) expect(cardKeysSeen(side, down).whole).toBe(true)
    // The numbers the ruling rests on: the lens is 30.5 by 20.5 degrees to each side; at the build's height (12.5 down
    // since M87) the keys' foot is 0.3 degrees inside it; 26 down they reach 33.1.
    const near = cardKeysSeen(CARD.outYawDeg, KEYS.standPitchDeg)
    expect(near.left).toBeGreaterThan(-PLACES.viewHalfYaw)
    expect(near.right).toBeLessThan(PLACES.viewHalfYaw)
    expect(PLACES.viewHalfPitch + near.bottom).toBeGreaterThan(0.2)
    expect(PLACES.viewHalfPitch + near.bottom).toBeLessThan(0.5)
    expect(near.top).toBeLessThan(0)
    expect(+cardKeysSeen(0, -26).bottom.toFixed(1)).toBe(-33.1)
    for (const side of [0, CARD.outYawDeg, -CARD.outYawDeg]) {
      const low = cardKeysSeen(side, -26)
      expect(low.whole).toBe(false)
      expect(low.bottom).toBeLessThan(-PLACES.viewHalfPitch)
    }
  })
})

describe('the keys\' line stands a degree and more under the card at its place (M87)', () => {
  it('reads the numbers from the build: the card\'s lower edge at -3.5 degrees', () => {
    expect(cardLowDeg()).toBeCloseTo(-3.49, 1)
  })
  it('puts the line\'s top at least 1 degree under the card\'s lower edge, at the card\'s own side', () => {
    expect(cardLowDeg() - keysLineTopDeg(KEYS.standPitchDeg)).toBeGreaterThanOrEqual(1)
  })
  it('at the old -11 degrees the line reached into the card', () => {
    expect(keysLineTopDeg(-11)).toBeGreaterThan(cardLowDeg())
  })
})
