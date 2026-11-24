import { describe, expect, it } from 'vitest'
import { Group, Mesh } from 'three'
import { DESK_FRONT, PRESS } from './drawOrder'
import { botOf, frameFirst, hitKind } from './frameFirst'

const under = (userData: Record<string, unknown>, order?: number) => {
  const g = new Group(); g.userData = userData
  const m = new Mesh(); g.add(m)
  if (order !== undefined) (m as Mesh & { pointerEventsOrder?: number }).pointerEventsOrder = order
  return m
}

describe("the dot's choice between two hits (D78 1)", () => {
  const frame = { object: under({ xrFrame: 'page' }), distance: 1.8 }
  const bot = { object: under({ xrPress: 'bot', xrBot: 'atlas' }), distance: 1.2 }
  const deskBoard = { object: under({ xrPress: 'board' }, 1), distance: 1.3 }
  const heldBoard = { object: under({ xrPress: 'board', xrHeld: true }), distance: 0.6 }
  const content = { object: under({ xrPage: 'page' }), distance: 1.8 }
  const home = { object: under({ xrPress: 'home' }), distance: 1.7 }

  it("takes a page's frame before a Bot or the board at its desk that stands nearer", () => {
    expect(frameFirst(frame, undefined, bot, undefined)).toBeLessThan(0)
    expect(frameFirst(bot, undefined, frame, undefined)).toBeGreaterThan(0)
    expect(frameFirst(frame, undefined, deskBoard, 1)).toBeLessThan(0)
    // Any part of a board at a desk (its tag, with no order of its own), as far as a desk is.
    expect(frameFirst(frame, undefined, { object: under({ xrPress: 'board' }), distance: 1.3 }, undefined)).toBeLessThan(0)
    expect(botOf(bot.object)).toBe('atlas')
  })

  it('takes Home before any part of a page that stands nearer, and a held board before Home, by the one order', () => {
    const nearPage = { object: under({ xrPage: 'page' }), distance: 0.5 }
    const nearFrame = { object: under({ xrFrame: 'page' }), distance: 0.5 }
    expect(frameFirst(home, PRESS.home, nearPage, undefined)).toBeLessThan(0)
    expect(frameFirst(nearFrame, undefined, home, PRESS.home)).toBeGreaterThan(0)
    expect(frameFirst(heldBoard, PRESS.held, home, PRESS.home)).toBeLessThan(0)
    expect(frameFirst(home, PRESS.home, heldBoard, PRESS.held)).toBeGreaterThan(0)
    // Home before the board at a desk, which it is drawn over.
    expect(frameFirst(home, PRESS.home, deskBoard, DESK_FRONT)).toBeLessThan(0)
  })

  it('knows a held board by being held, not by its distance (M39)', () => {
    // The second of two held boards stands 0.80 m off; a part of it off the straight line is farther.
    const backBoard = { object: under({ xrPress: 'board', xrHeld: true }), distance: 0.85 }
    expect(hitKind(backBoard.object)).toBe('other')
    expect(frameFirst(backBoard, PRESS.held, frame, undefined)).toBeLessThan(0)
    expect(frameFirst(frame, undefined, backBoard, PRESS.held)).toBeGreaterThan(0)
    // With no order at all it is still nearer than the frame behind it.
    expect(frameFirst(backBoard, undefined, frame, undefined)).toBeLessThan(0)
    // A board at its Bot is the room's, near or far.
    expect(hitKind(under({ xrPress: 'board', xrHeld: false }))).toBe('bot')
  })

  it("leaves a Bot in front of a page's content its pinch, and a held board and Home before a frame", () => {
    expect(frameFirst(bot, undefined, content, undefined)).toBeLessThan(0)
    expect(frameFirst(heldBoard, PRESS.held, frame, undefined)).toBeLessThan(0)
    expect(frameFirst(home, PRESS.home, frame, undefined)).toBeLessThan(0)
    expect(hitKind(heldBoard.object)).toBe('other')
    // The higher order still goes first between other things.
    expect(frameFirst(content, undefined, { object: under({ xrPress: 'plate' }), distance: 2 }, 1)).toBeGreaterThan(0)
  })
})
