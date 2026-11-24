import { describe, expect, it } from 'vitest'
import { leaveEndsPress, visibilityTakes } from './botPress'

describe('the leave of a press on a Bot (M166)', () => {
  const press = { pointerId: 7, lifted: false, holding: false }
  // A session gives the Crew no `drag` (`EmptySpace.tsx`: `children?.(undefined)`); the flat preview gives it.
  it.each([
    { name: 'drag given (the preview): the press goes on', drag: true, p: press, ends: false },
    { name: 'no drag (a session), a plain press: the press ends (M203)', drag: false, p: press, ends: true },
    { name: 'a lifted Bot goes on', drag: false, p: { ...press, lifted: true }, ends: false },
    { name: 'a hold goes on', drag: false, p: { ...press, holding: true }, ends: false },
  ])('$name', ({ drag, p, ends }) => {
    expect(leaveEndsPress(p, 7, { drag })).toBe(ends)
  })
  it('a leave of another pointer, or with no press, ends nothing', () => {
    expect(leaveEndsPress(press, 8, { drag: false })).toBe(false)
    expect(leaveEndsPress(null, 7, { drag: false })).toBe(false)
  })
})

describe('which change of the session\'s visibility takes a press (D185, M223)', () => {
  it.each([
    { name: 'the first read, at the mount, visible: nothing', prev: null, now: 'visible', takes: false },
    { name: 'visible to hidden', prev: 'visible', now: 'hidden', takes: true },
    { name: 'visible to visible-blurred', prev: 'visible', now: 'visible-blurred', takes: true },
    { name: 'the session ends (the store reads nothing)', prev: 'visible', now: undefined, takes: true },
    { name: 'a session begins (M223)', prev: undefined, now: 'visible', takes: true },
    { name: 'back to visible from a layer: nothing more', prev: 'hidden', now: 'visible', takes: false },
    { name: 'visible to visible (V2: a store change that keeps it): nothing', prev: 'visible', now: 'visible', takes: false },
  ])('$name', ({ prev, now, takes }) => {
    expect(visibilityTakes(prev, now)).toBe(takes)
  })
})
