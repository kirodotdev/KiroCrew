/**
 * Pack manifest parsing, serialization and structural validation, plus the SVG and Lottie
 * content checks. Each case is a pure call on the function under test, asserted on its result.
 */
import { describe, expect, it } from 'vitest'

import {
  isValidLottie,
  isValidSvg,
  parseManifest,
  serializeManifest,
  validateManifestStructure,
} from './appearanceTypes'

const STATES = { idle: 'a.svg', walking: 'b.svg', thinking: 'c.svg', working: 'd.svg', error: 'e.svg', offline: 'f.svg' }
const manifest = () => ({
  meta: { id: 'p1', name: 'Pack', author: 'me', type: 'custom', format: 'svg', thumbnail: 't.png' },
  states: { ...STATES },
  moods: {},
})
const json = (m: unknown) => JSON.stringify(m)

describe('serializeManifest and parseManifest', () => {
  it('serializes with two-space indent and round-trips through parse', () => {
    const text = serializeManifest(manifest() as never)
    expect(text).toContain('\n  "meta"')
    expect(parseManifest(text)).toEqual({ ok: true, value: manifest() })
  })

  it('rejects bad JSON, non-object roots and arrays', () => {
    expect(parseManifest('{nope')).toEqual({ ok: false, error: 'Invalid JSON' })
    expect(parseManifest('null')).toEqual({ ok: false, error: 'Manifest must be a JSON object' })
    expect(parseManifest('[]')).toEqual({ ok: false, error: 'Manifest must be a JSON object' })
  })

  it('names the first missing meta, state or moods section it finds', () => {
    const noMeta = { ...manifest(), meta: [] }
    expect(parseManifest(json(noMeta))).toEqual({ ok: false, error: 'Missing or invalid "meta" field' })
    const noName = { ...manifest(), meta: { ...manifest().meta, name: 3 } }
    expect(parseManifest(json(noName))).toEqual({ ok: false, error: 'Missing or invalid meta field: "name"' })
    const noStates = { ...manifest(), states: null }
    expect(parseManifest(json(noStates))).toEqual({ ok: false, error: 'Missing or invalid "states" field' })
    const noOffline = { ...manifest(), states: { ...STATES, offline: undefined } }
    expect(parseManifest(json(noOffline))).toEqual({ ok: false, error: 'Missing or invalid state mapping: "offline"' })
    const noMoods = { ...manifest(), moods: 'none' }
    expect(parseManifest(json(noMoods))).toEqual({ ok: false, error: 'Missing or invalid "moods" field' })
  })

  it('keeps a sprite config only when it is an object', () => {
    const withSprite = parseManifest(json({ ...manifest(), sprite: { cols: 4 } }))
    expect(withSprite.ok && withSprite.value.sprite).toEqual({ cols: 4 })
    const nullSprite = parseManifest(json({ ...manifest(), sprite: null }))
    expect(nullSprite.ok).toBe(true)
    expect(nullSprite.ok && nullSprite.value.sprite).toBeNull()
  })
})

describe('validateManifestStructure', () => {
  it('returns no errors for a complete manifest', () => {
    expect(validateManifestStructure(manifest())).toEqual([])
  })

  it('reports a non-object root as the only error', () => {
    expect(validateManifestStructure(['x'])).toEqual(['Manifest must be a non-null object'])
    expect(validateManifestStructure(null)).toEqual(['Manifest must be a non-null object'])
  })

  it('reports a missing meta object, and empty or missing meta fields', () => {
    expect(validateManifestStructure({ ...manifest(), meta: null })).toEqual(['Missing or invalid "meta" field'])
    const emptyId = { ...manifest(), meta: { ...manifest().meta, id: '' } }
    expect(validateManifestStructure(emptyId)).toEqual(['Missing or invalid meta field: "id"'])
  })

  it('checks the meta type and format against their enums', () => {
    const badType = { ...manifest(), meta: { ...manifest().meta, type: 'shared' } }
    expect(validateManifestStructure(badType)).toEqual([
      'Invalid meta.type: "shared" (expected "built-in" or "custom")',
    ])
    const badFormat = { ...manifest(), meta: { ...manifest().meta, format: 'gif' } }
    expect(validateManifestStructure(badFormat)).toEqual([
      'Invalid meta.format: "gif" (expected "svg", "lottie", or "sprite")',
    ])
  })

  it('reports missing states, empty state mappings and a missing moods section', () => {
    expect(validateManifestStructure({ ...manifest(), states: [] })).toEqual(['Missing or invalid "states" field'])
    const emptyIdle = { ...manifest(), states: { ...STATES, idle: '' } }
    expect(validateManifestStructure(emptyIdle)).toEqual(['Missing or invalid state: "idle"'])
    const { moods: _moods, ...noMoods } = manifest()
    expect(validateManifestStructure(noMoods)).toEqual(['Missing or invalid "moods" field'])
  })
})

describe('isValidSvg and isValidLottie', () => {
  it('finds an svg opening tag in any letter case', () => {
    expect(isValidSvg('<SVG viewBox="0 0 1 1"></SVG>')).toBe(true)
    expect(isValidSvg('<div></div>')).toBe(false)
  })

  it('accepts Lottie JSON only with every top-level Lottie key', () => {
    const full = { v: '5', fr: 30, ip: 0, op: 60, layers: [] }
    expect(isValidLottie(json(full))).toBe(true)
    expect(isValidLottie(json({ ...full, layers: undefined, op: undefined }))).toBe(false)
    expect(isValidLottie('{broken')).toBe(false)
    expect(isValidLottie('[]')).toBe(false)
    expect(isValidLottie('null')).toBe(false)
  })
})
