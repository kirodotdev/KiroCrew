import { describe, expect, it, vi } from 'vitest'
import { AIM_REST_MS, DISARM_MS, HOLD_MS } from './input'
import { CARD, CARD_FONT, COMMAND_DEG, FULL, FULL_FONT, STRIP, TEXT_FLOOR_DEG, BODY_DEG, degOf, pxPerDeg } from './metrics'
import { PressRouter, type RouteTarget } from './router'
import { isPuck, sourceIdFor } from './sources'

/**
 * These tests drive the route's own action handling: the object the scene
 * hands every press to. `write` is a spy, so each test says whether a press
 * reached the write path, not what a helper reported about it.
 */
const approve: RouteTarget = { id: 'full:1/approve:a:w:req-1', guarded: true, version: 'req-1|m1|{"command":"npm test"}' }
const plain: RouteTarget = { id: 'bot:atlas' }
const T0 = 10_000

function route() {
  const hooks = { act: vi.fn(), write: vi.fn(), refused: vi.fn() }
  const r = new PressRouter(hooks)
  // The card with Approve has been on show long enough to be armed.
  r.sync([approve], T0 - DISARM_MS - 1)
  return { r, ...hooks }
}

/** Aim, rest, press, hold, release: the press that is meant to count. */
function holdOn(r: PressRouter, source: string, target = approve, t = T0) {
  r.hover(source, target.id, t)
  r.down(source, target, t + AIM_REST_MS)
  return r.up(source, target.id, t + AIM_REST_MS + HOLD_MS)
}

describe('the route: what a press reaches', () => {
  it('a plain press acts and never writes', () => {
    const { r, act, write } = route()
    r.down('mouse', plain, T0)
    r.up('mouse', plain.id, T0 + 20)
    expect(act).toHaveBeenCalledWith('bot:atlas')
    expect(write).not.toHaveBeenCalled()
  })

  it('aim, rest, hold and release on Approve writes, with the request it began on', () => {
    const { r, write, act } = route()
    holdOn(r, 'hand-right')
    expect(write).toHaveBeenCalledTimes(1)
    expect(write).toHaveBeenCalledWith(approve.id, approve.version)
    expect(act).not.toHaveBeenCalled()
  })

  it('a quick pinch on Approve never reaches the write path', () => {
    const { r, write, refused } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.up('hand-right', approve.id, T0 + AIM_REST_MS + 80)
    expect(write).not.toHaveBeenCalled()
    expect(refused).toHaveBeenCalledWith(approve.id, 'too-short')
  })

  it('a pinch that was closed before the aim rested never writes', () => {
    const { r, write, refused } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + 50)
    r.up('hand-right', approve.id, T0 + 50 + HOLD_MS + 100)
    expect(write).not.toHaveBeenCalled()
    expect(refused).toHaveBeenCalledWith(approve.id, 'no-aim')
  })

  it('a press the system ended without a select never writes', () => {
    const { r, write } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.up('hand-right', approve.id, T0 + AIM_REST_MS + HOLD_MS, false)
    expect(write).not.toHaveBeenCalled()
  })

  it('a card that just appeared cannot be approved yet', () => {
    const hooks = { act: vi.fn(), write: vi.fn(), refused: vi.fn() }
    const r = new PressRouter(hooks)
    r.sync([approve], T0)
    holdOn(r, 'hand-right', approve, T0)
    expect(hooks.write).not.toHaveBeenCalled()
    expect(hooks.refused).toHaveBeenCalledWith(approve.id, 'disarmed')
  })

  it('a request that changes under a held press is not approved', () => {
    const { r, write, refused } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.sync([{ ...approve, version: 'req-2|m2|{"command":"rm -rf build"}' }], T0 + AIM_REST_MS + 200)
    r.up('hand-right', approve.id, T0 + AIM_REST_MS + HOLD_MS)
    expect(write).not.toHaveBeenCalled()
    expect(refused).toHaveBeenCalled()
  })

  it('a card that goes away under a held press is not approved', () => {
    const { r, write } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.sync([], T0 + AIM_REST_MS + 200)
    r.up('hand-right', approve.id, T0 + AIM_REST_MS + HOLD_MS)
    expect(write).not.toHaveBeenCalled()
  })

  it.each(['blur', 'hidden', 'tracking-lost', 'session-end', 'system', 'scrolled'] as const)('%s during the hold stops the write', reason => {
    const { r, write } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.cancel(reason)
    r.up('hand-right', approve.id, T0 + AIM_REST_MS + HOLD_MS)
    expect(write).not.toHaveBeenCalled()
  })

  it('a second hand pressing stops the write, and its own press does not act either', () => {
    const { r, write, act } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.down('hand-left', plain, T0 + AIM_REST_MS + 100)
    r.up('hand-left', plain.id, T0 + AIM_REST_MS + 150)
    r.up('hand-right', approve.id, T0 + AIM_REST_MS + HOLD_MS)
    expect(write).not.toHaveBeenCalled()
    expect(act).not.toHaveBeenCalled()
  })

  it('releasing somewhere else never writes', () => {
    const { r, write } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.hover('hand-right', 'full:1/need:x', T0 + AIM_REST_MS + 300)
    r.up('hand-right', 'full:1/need:x', T0 + AIM_REST_MS + HOLD_MS)
    expect(write).not.toHaveBeenCalled()
  })

  it('a device can press plain targets and can never approve', () => {
    const { r, write, act, refused } = route()
    const puck = sourceIdFor({ handedness: 'right', targetRayMode: 'tracked-pointer', profiles: ['generic-trigger'] }, 0)
    holdOn(r, puck)
    expect(write).not.toHaveBeenCalled()
    expect(refused).toHaveBeenCalledWith(approve.id, 'puck')
    r.down(puck, plain, T0 + 5000)
    r.up(puck, plain.id, T0 + 5020)
    expect(act).toHaveBeenCalledWith('bot:atlas')
  })

  it('the keyboard is held to the same press: a tap of Enter sends nothing, a hold does', () => {
    const send: RouteTarget = { id: 'draft/send', guarded: true, version: 'juno|status?' }
    const hooks = { act: vi.fn(), write: vi.fn(), refused: vi.fn() }
    const r = new PressRouter(hooks)
    r.sync([send], T0 - DISARM_MS - 1)
    r.hover('key', send.id, T0 - AIM_REST_MS)
    r.down('key', send, T0)
    r.up('key', send.id, T0 + 90)
    expect(hooks.write).not.toHaveBeenCalled()
    r.hover('key', send.id, T0 + 1000 - AIM_REST_MS)
    r.down('key', send, T0 + 1000)
    r.up('key', send.id, T0 + 1000 + HOLD_MS)
    expect(hooks.write).toHaveBeenCalledWith('draft/send', 'juno|status?')
  })

  it('a draft that was edited after the press began is not sent', () => {
    const send: RouteTarget = { id: 'draft/send', guarded: true, version: 'juno|go' }
    const hooks = { act: vi.fn(), write: vi.fn(), refused: vi.fn() }
    const r = new PressRouter(hooks)
    r.sync([send], T0 - DISARM_MS - 1)
    r.hover('hand-right', send.id, T0)
    r.down('hand-right', send, T0 + AIM_REST_MS)
    r.sync([{ ...send, version: 'juno|go ahead and delete it' }], T0 + AIM_REST_MS + 100)
    r.up('hand-right', send.id, T0 + AIM_REST_MS + HOLD_MS)
    expect(hooks.write).not.toHaveBeenCalled()
  })
})

describe('a press that is let go on purpose', () => {
  it('ends with no outcome: nothing is written, acted on or refused, and a later release finds no press', () => {
    const { r, act, write, refused } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.drop('hand-right')
    expect(r.pressedId).toBe('')
    expect(r.last).toBe('dropped')
    expect(r.up('hand-right', approve.id, T0 + AIM_REST_MS + HOLD_MS)).toEqual({ refused: 'no-press' })
    expect(write).not.toHaveBeenCalled()
    expect(act).not.toHaveBeenCalled()
    expect(refused).not.toHaveBeenCalled()
  })

  it('leaves another source\'s press alone', () => {
    const { r, write } = route()
    r.hover('hand-right', approve.id, T0)
    r.down('hand-right', approve, T0 + AIM_REST_MS)
    r.drop('hand-left')
    expect(r.up('hand-right', approve.id, T0 + AIM_REST_MS + HOLD_MS)).toEqual({ commit: approve.id })
    expect(write).toHaveBeenCalledTimes(1)
  })
})

describe('who is pressing', () => {
  it('names a source by what the browser says it is', () => {
    expect(sourceIdFor({ handedness: 'right', hand: {}, targetRayMode: 'tracked-pointer' }, 0)).toBe('hand-right')
    expect(sourceIdFor({ handedness: 'left', profiles: ['generic-hand-select'] }, 1)).toBe('hand-left')
    expect(sourceIdFor({ handedness: 'none', targetRayMode: 'transient-pointer' }, 0)).toBe('pinch-0')
    expect(sourceIdFor({ handedness: 'right', targetRayMode: 'tracked-pointer', profiles: ['meta-quest-touch-plus'] }, 0)).toBe('puck-right')
    expect(sourceIdFor({ handedness: 'none', targetRayMode: 'gaze' }, 1)).toBe('puck-1')
  })

  it('gives a source it knows nothing about the least trust', () => {
    expect(isPuck(sourceIdFor(null, 0))).toBe(true)
    expect(isPuck(sourceIdFor(undefined, 1))).toBe(true)
    expect(isPuck('hand-right')).toBe(false)
  })
})

describe('text size at the eye', () => {
  it('keeps every font of a full panel and a card at or above the reading floor', () => {
    for (const [spec, font] of [[FULL, FULL_FONT], [CARD, CARD_FONT]] as const) {
      expect(degOf(spec, font.small)).toBeGreaterThanOrEqual(TEXT_FLOOR_DEG)
      expect(degOf(spec, font.body)).toBeGreaterThanOrEqual(BODY_DEG)
      expect(degOf(spec, font.command)).toBeGreaterThanOrEqual(COMMAND_DEG)
    }
  })

  it('draws the command of an approval larger than body text', () => {
    expect(FULL_FONT.command).toBeGreaterThan(FULL_FONT.body)
  })

  it('keeps the hand strip labels readable at arm length', () => {
    expect(degOf(STRIP, 20)).toBeGreaterThanOrEqual(TEXT_FLOOR_DEG)
  })

  it('fits two full panels side by side in a 70 degree view', () => {
    const wide = FULL.px / pxPerDeg(FULL)
    expect(wide * 2 + 3).toBeLessThanOrEqual(62.8)
  })
})
