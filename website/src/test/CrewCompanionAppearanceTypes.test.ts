/**
 * appearanceTypes.ts — manifest (de)serialization and format validation.
 *
 * Previously untested; every expectation below is derived from the module's
 * actual logic. The sprite-drop branch in parseManifest is the one with a
 * documented bug history (a `"sprite": "nope"` string used to reach the
 * sprite renderer typed as SpriteConfig), so it gets explicit pins.
 */
import { describe, it, expect } from 'vitest'

import {
  isValidLottie,
  isValidSvg,
  parseManifest,
  serializeManifest,
  type PackManifest,
} from '../apps/crew-companion/appearanceTypes'

const META = {
  id: 'p1',
  name: 'Pack One',
  author: 'serena',
  type: 'custom',
  format: 'svg',
  thumbnail: 'idle.svg',
}

const VALID = {
  meta: META,
  states: { idle: 'idle.svg' },
  moods: {},
}

describe('parseManifest', () => {
  it('round-trips through serializeManifest', () => {
    const r = parseManifest(serializeManifest(VALID as unknown as PackManifest))
    expect(r.ok).toBe(true)
  })

  it('rejects non-JSON, non-object, and array roots with distinct errors', () => {
    expect(parseManifest('{nope')).toEqual({ ok: false, error: 'Invalid JSON' })
    expect(parseManifest('"str"')).toEqual({ ok: false, error: 'Manifest must be a JSON object' })
    expect(parseManifest('[]')).toEqual({ ok: false, error: 'Manifest must be a JSON object' })
  })

  it('names the missing meta field in its error', () => {
    const { author: _drop, ...partial } = META
    const r = parseManifest(JSON.stringify({ ...VALID, meta: partial }))
    expect(r).toEqual({ ok: false, error: 'Missing or invalid meta field: "author"' })
  })

  it('requires every REQUIRED_STATES mapping to be a string', () => {
    const r = parseManifest(JSON.stringify({ ...VALID, states: { idle: 42 } }))
    expect(r.ok).toBe(false)
  })

  it('requires moods to be an object, even when empty', () => {
    const r = parseManifest(JSON.stringify({ ...VALID, moods: 'nope' }))
    expect(r).toEqual({ ok: false, error: 'Missing or invalid "moods" field' })
  })

  it('DROPS a sprite field that is not an object (the renderer-crash pin)', () => {
    const r = parseManifest(JSON.stringify({ ...VALID, sprite: 'nope' }))
    expect(r.ok).toBe(true)
    if (r.ok) expect('sprite' in r.value).toBe(false)
  })

  it('keeps a sprite field that IS an object', () => {
    const sprite = { frameWidth: 32, frameHeight: 32, fps: 8 }
    const r = parseManifest(JSON.stringify({ ...VALID, sprite }))
    expect(r.ok).toBe(true)
    if (r.ok) expect(r.value.sprite).toEqual(sprite)
  })
})

describe('format validation', () => {
  it('isValidSvg is a case-insensitive tag sniff', () => {
    expect(isValidSvg('<SVG xmlns="...">')).toBe(true)
    expect(isValidSvg('   <svg/>')).toBe(true)
    expect(isValidSvg('<div/>')).toBe(false)
  })

  it('isValidLottie needs all five Lottie fields on a JSON object root', () => {
    const lottie = { v: '5.7', fr: 30, ip: 0, op: 60, layers: [] }
    expect(isValidLottie(JSON.stringify(lottie))).toBe(true)
    const { layers: _drop, ...partial } = lottie
    expect(isValidLottie(JSON.stringify(partial))).toBe(false)
    expect(isValidLottie('not json')).toBe(false)
    expect(isValidLottie('[1,2]')).toBe(false)
  })
})
