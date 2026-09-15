/**
 * The slot fallback chain, and its parity with the server's.
 *
 * A pack's FORMAT lives on the resolved slot, so the client has to walk the chain
 * itself before it can pick a player — which means the chain exists twice, here
 * and in `dashboard/appearances.py`. This test reads the backend's own order out
 * of that module and compares it, so the duplicate cannot drift into a client
 * that hands a Lottie parser a PNG.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { packDetailFrom, resolveSlot, spriteRowFor } from '../lib/appearancePacks/detail'

const svg = { content: '<svg/>', format: 'svg' }

describe('resolveSlot', () => {
  const full = packDetailFrom({
    animations: { idle: svg, loading: svg, thinking: svg, working: svg, done: svg, error: svg },
  })

  it('prefers the state own slot when the pack draws it', () => {
    for (const state of ['idle', 'working', 'done', 'error']) {
      expect(resolveSlot(full, state)).toBe(state)
    }
  })

  it('walks working through loading and thinking before idle', () => {
    const noWorking = packDetailFrom({ animations: { idle: svg, loading: svg, thinking: svg } })
    expect(resolveSlot(noWorking, 'working')).toBe('loading')
    expect(resolveSlot(packDetailFrom({ animations: { idle: svg, thinking: svg } }), 'working'))
      .toBe('thinking')
    expect(resolveSlot(packDetailFrom({ animations: { idle: svg } }), 'working')).toBe('idle')
  })

  it('falls done and error back to idle', () => {
    const idleOnly = packDetailFrom({ animations: { idle: svg } })
    expect(resolveSlot(idleOnly, 'done')).toBe('idle')
    expect(resolveSlot(idleOnly, 'error')).toBe('idle')
  })

  it('resolves an author-named clip to itself only', () => {
    // A pack's random clips are named by their author, so a fixed vocabulary
    // would make them unfetchable — and `walking` must not silently draw idle.
    const pack = packDetailFrom({ animations: { idle: svg, walking: svg } })
    expect(resolveSlot(pack, 'walking')).toBe('walking')
    expect(resolveSlot(packDetailFrom({ animations: { idle: svg } }), 'walking')).toBeNull()
  })

  it('answers null for a pack that draws nothing at all', () => {
    expect(resolveSlot(packDetailFrom({}), 'idle')).toBeNull()
  })
})

describe('chain parity with the server', () => {
  it('matches the order dashboard/appearances.py resolves', () => {
    // Read the backend's own table rather than restating it: a chain changed
    // there and not here is the drift this test exists for.
    const py = readFileSync(
      resolve(__dirname, '../../../src/kiro_crew/dashboard/appearances.py'),
      'utf-8',
    )
    for (const [state, chain] of [
      ['working', ['working', 'loading', 'thinking', 'idle']],
      ['done', ['done', 'idle']],
      ['error', ['error', 'idle']],
    ] as const) {
      // The module spells each chain as a tuple/list of slot names beside its
      // state key; assert every member appears in order within one line of it.
      const line = py.split('\n').find((l) => l.includes(`"${state}"`) && l.includes('"idle"'))
      expect(line, `no fallback line for ${state}`).toBeTruthy()
      let at = -1
      for (const slot of chain) {
        const next = line!.indexOf(`"${slot}"`, at + 1)
        expect(next, `${state} -> ${slot}`).toBeGreaterThan(at)
        at = next
      }
    }
  })
})

describe('spriteRowFor', () => {
  const sheet = (rowAssignments: Record<string, number>) =>
    packDetailFrom({
      animations: { idle: { content: 'b64', format: 'sprite' } },
      sprite: { frameWidth: 16, frameHeight: 16, fps: 4, rowAssignments },
    })

  it('reads the row the sheet assigns the slot', () => {
    expect(spriteRowFor(sheet({ idle: 0, working: 2 }), 'working')).toBe(2)
  })

  it('treats an unassigned slot as a single-row strip', () => {
    expect(spriteRowFor(sheet({ working: 1 }), 'idle')).toBe(0)
    expect(spriteRowFor(packDetailFrom({ animations: {} }), 'idle')).toBe(0)
  })

  it('refuses a row that would sample outside the sheet', () => {
    // The map comes out of a third-party bundle; a negative offset samples above
    // the sheet and draws an empty frame for every state.
    expect(spriteRowFor(sheet({ idle: -3 }), 'idle')).toBe(0)
    expect(spriteRowFor(sheet({ idle: 1.7 }), 'idle')).toBe(1)
    expect(spriteRowFor(sheet({ idle: NaN }), 'idle')).toBe(0)
  })
})

describe('packDetailFrom', () => {
  it('drops a malformed slot rather than defaulting it', () => {
    // A dropped slot lets the chain step PAST it; a defaulted one would hand a
    // player bytes it cannot read.
    const detail = packDetailFrom({
      animations: { idle: svg, working: { format: 'lottie' }, done: { content: '' } },
    })
    expect(Object.keys(detail.animations)).toEqual(['idle'])
    expect(resolveSlot(detail, 'working')).toBe('idle')
  })

  it('reads an unknown format as svg', () => {
    const detail = packDetailFrom({ animations: { idle: { content: 'x', format: 'webp' } } })
    expect(detail.animations.idle.format).toBe('svg')
  })

  it('keeps the bytes only for the player that parses them here', () => {
    // The detail route inlines every file. A lottie document is parsed in the
    // page, so its bytes stay; an svg is drawn by an <img> on the slot route and
    // a sheet by a canvas from the same route, so theirs would only pin a base64
    // sheet in the cache for the session.
    const detail = packDetailFrom({
      animations: {
        idle: { content: '{"v":"5.7"}', format: 'lottie' },
        working: { content: '<svg/>', format: 'svg' },
        done: { content: 'iVBORw0KGgo=', format: 'sprite' },
      },
    })
    expect(detail.animations.idle.content).toBe('{"v":"5.7"}')
    expect(detail.animations.working).toEqual({ content: '', format: 'svg' })
    expect(detail.animations.done).toEqual({ content: '', format: 'sprite' })
    // Still a slot the chain can land on.
    expect(resolveSlot(detail, 'done')).toBe('done')
  })

  it('is total against junk', () => {
    for (const junk of [null, undefined, 42, 'nope', [], { animations: 'nope' }]) {
      expect(packDetailFrom(junk)).toEqual({ animations: {}, sprite: undefined })
    }
  })

  describe('sprite geometry', () => {
    // `SpriteRenderer` divides the sheet width by `frameWidth` to count frames,
    // so a served `"0"` is Infinity frames and a scan that never ends. The
    // config is a third-party bundle's own words, kept verbatim by the store;
    // this is where it stops being trusted.
    const withSprite = (sprite: unknown) =>
      packDetailFrom({ animations: { idle: svg }, sprite }).sprite

    it('keeps whole-pixel dimensions and a positive finite rate', () => {
      expect(withSprite({ frameWidth: 16, frameHeight: 24, fps: 12.5, rowAssignments: { idle: 1 } }))
        .toEqual({ frameWidth: 16, frameHeight: 24, fps: 12.5, rowAssignments: { idle: 1 } })
    })

    it('drops a fractional dimension rather than rounding it into a different cut', () => {
      // 16.5 cuts the sheet wrong at 16 and at 17 alike; the renderer's default
      // is at least a known answer.
      const sprite = withSprite({ frameWidth: 16.5, frameHeight: 24.9, fps: 8 })
      expect(sprite?.frameWidth).toBeUndefined()
      expect(sprite?.frameHeight).toBeUndefined()
    })

    it.each([
      ['a string', '0'],
      ['zero', 0],
      ['a negative', -16],
      ['NaN', NaN],
      ['Infinity', Infinity],
      ['a fraction below one', 0.5],
      ['a fraction above one', 16.5],
      ['a boolean', true],
      ['absent', undefined],
    ])('drops a width that is %s rather than passing it to the renderer', (_label, raw) => {
      const sprite = withSprite({ frameWidth: raw, frameHeight: 16, fps: 8 })
      expect(sprite?.frameWidth).toBeUndefined()
      expect(sprite?.frameHeight).toBe(16)
    })

    it('drops a rate that is not a positive finite number', () => {
      for (const raw of ['8', 0, -1, NaN, Infinity, null]) {
        expect(withSprite({ frameWidth: 16, frameHeight: 16, fps: raw })?.fps).toBeUndefined()
      }
    })

    it('drops a row map that is not an object', () => {
      expect(withSprite({ frameWidth: 16, frameHeight: 16, fps: 8, rowAssignments: [0, 1] })?.rowAssignments)
        .toBeUndefined()
      expect(withSprite({ frameWidth: 16, frameHeight: 16, fps: 8, rowAssignments: 'idle' })?.rowAssignments)
        .toBeUndefined()
    })

    it('reads no config at all from a sprite that is not an object', () => {
      expect(withSprite('sprite')).toBeUndefined()
      expect(withSprite([16, 16])).toBeUndefined()
    })
  })
})
