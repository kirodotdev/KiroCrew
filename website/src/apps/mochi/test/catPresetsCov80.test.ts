/**
 * The preset data model: validation, the registry, and the appearance
 * description the agent is handed.
 *
 * `validatePreset` is the gate a user-authored preset passes through before it is
 * persisted, and `PresetRegistry`'s constructor filter is what keeps a corrupted
 * settings file from putting `undefined` in the picker — both are error paths, so
 * nothing but a test exercises them.
 */
import { describe, it, expect } from 'vitest'

import {
  PresetRegistry,
  extractSwatches,
  generatePresetId,
  type CatPreset,
  validatePreset,
} from '../src/shared/catPresets'
import {
  BUILT_IN_CAT_PRESETS,
  describeAppearance,
  presetDisplayName,
} from '../src/shared/builtInCatPresets'

function preset(over: Partial<CatPreset> = {}): CatPreset {
  return {
    id: 'zzq-1',
    name: 'zzq name',
    description: '',
    colorMap: { '#F9A85F': '#112233' },
    swatches: ['#112233', '#445566'],
    builtIn: false,
    ...over,
  }
}

describe('validatePreset', () => {
  it('accepts a well-formed preset', () => {
    expect(validatePreset(preset())).toEqual([])
  })

  it('requires an id and a name', () => {
    const errors = validatePreset(preset({ id: '', name: '' }))
    expect(errors).toContain('id is required')
    expect(errors).toContain('name is required')
  })

  it('rejects a non-hex colorMap key and value separately', () => {
    const errors = validatePreset(preset({ colorMap: { orange: 'blue' } }))
    expect(errors).toContain('invalid colorMap key: orange')
    expect(errors).toContain('invalid colorMap value: blue')
  })

  it('bounds the swatch count at 2–5 and names the count it got', () => {
    expect(validatePreset(preset({ swatches: ['#112233'] }))).toContain(
      'swatches length must be 2-5, got 1',
    )
    expect(
      validatePreset(preset({ swatches: ['#1', '#2', '#3', '#4', '#5', '#6'] })).some((e) =>
        e.startsWith('swatches length must be 2-5, got 6'),
      ),
    ).toBe(true)
  })

  it('rejects a non-hex swatch', () => {
    expect(validatePreset(preset({ swatches: ['#112233', 'nope'] }))).toContain(
      'invalid swatch: nope',
    )
  })
})

describe('extractSwatches', () => {
  it('keeps the first five distinct valid colors in order', () => {
    const out = extractSwatches({
      a: '#111111',
      b: '#111111',
      c: 'not-a-color',
      d: '#222222',
      e: '#333333',
      f: '#444444',
      g: '#555555',
      h: '#666666',
    })
    expect(out).toEqual(['#111111', '#222222', '#333333', '#444444', '#555555'])
  })

  it('is empty when nothing in the map is a color', () => {
    expect(extractSwatches({ a: 'blue' })).toEqual([])
  })
})

describe('generatePresetId', () => {
  it('is prefixed and does not repeat within a tick', () => {
    const a = generatePresetId()
    const b = generatePresetId()
    expect(a.startsWith('custom-')).toBe(true)
    expect(a).not.toBe(b)
  })
})

describe('PresetRegistry', () => {
  const builtIn = [preset({ id: 'b-1', builtIn: true })]

  it('drops malformed custom entries rather than listing holes', () => {
    const reg = new PresetRegistry(builtIn, [
      preset({ id: 'c-1' }),
      null as unknown as CatPreset,
      preset({ id: '' }),
      { id: 7 } as unknown as CatPreset,
    ])
    expect(reg.getCustomPresets().map((p) => p.id)).toEqual(['c-1'])
  })

  it('defaults to no custom presets', () => {
    const reg = new PresetRegistry(builtIn)
    expect(reg.getCustomPresets()).toEqual([])
    expect(reg.getAllPresets()).toHaveLength(1)
  })

  it('lists built-ins first and hands back copies, not its own arrays', () => {
    const reg = new PresetRegistry(builtIn, [preset({ id: 'c-1' })])
    expect(reg.getAllPresets().map((p) => p.id)).toEqual(['b-1', 'c-1'])
    reg.getBuiltInPresets().push(preset({ id: 'intruder' }))
    expect(reg.getBuiltInPresets().map((p) => p.id)).toEqual(['b-1'])
  })

  it('finds by id across both lists and answers null for an unknown one', () => {
    const reg = new PresetRegistry(builtIn, [preset({ id: 'c-1' })])
    expect(reg.getPresetById('b-1')?.id).toBe('b-1')
    expect(reg.getPresetById('c-1')?.id).toBe('c-1')
    expect(reg.getPresetById('nope')).toBeNull()
  })

  it('adds a custom preset under a generated id and marks it not built-in', () => {
    const reg = new PresetRegistry(builtIn)
    const id = reg.addCustomPreset({
      name: 'zzq added',
      description: '',
      colorMap: {},
      swatches: ['#111111', '#222222'],
    })
    expect(id.startsWith('custom-')).toBe(true)
    expect(reg.getPresetById(id)?.builtIn).toBe(false)
  })

  it('removes a custom preset but refuses to remove a built-in one', () => {
    const reg = new PresetRegistry(builtIn, [preset({ id: 'c-1' })])
    expect(reg.removeCustomPreset('b-1')).toBe(false)
    expect(reg.removeCustomPreset('nope')).toBe(false)
    expect(reg.removeCustomPreset('c-1')).toBe(true)
    expect(reg.getCustomPresets()).toEqual([])
  })
})

describe('describeAppearance', () => {
  it('describes the default look when no colors were remapped', () => {
    expect(describeAppearance(null, {})).toBe('Default orange tabby cat')
  })

  it('names the preset and lists only the parts that actually changed', () => {
    const out = describeAppearance('Russian Blue', {
      '#F9A85F': '#8BA4B8',
      // Mapped to ITSELF: unchanged, so it must not be described.
      '#F18D50': '#F18D50',
      '#522210': '#283848',
    })
    expect(out.startsWith('Russian Blue cat — ')).toBe(true)
    expect(out).toContain('body/fur: #8BA4B8')
    expect(out).toContain('outlines/eyes/mouth: #283848')
    expect(out).not.toContain('ears/shadow')
  })

  it('falls back to the custom-cat prefix with no preset name', () => {
    expect(describeAppearance(null, { '#F9A85F': '#8BA4B8' })).toContain('Custom colored cat')
  })

  it('returns the prefix alone when every key changed nothing describable', () => {
    // A remap of a colour that is not one of the key parts: real, but nothing
    // the description covers — the prefix must still come back on its own.
    expect(describeAppearance('Tabby', { '#391F19': '#000000' })).toBe('Tabby cat')
  })
})

describe('presetDisplayName', () => {
  it('translates a built-in id through the catalog', () => {
    const russian = BUILT_IN_CAT_PRESETS.find((p) => p.id === 'russian-blue')!
    expect(presetDisplayName(russian)).toBe('Russian Blue')
  })

  it('uses the typed-in name for a custom preset', () => {
    expect(presetDisplayName(preset({ name: 'zzq mine' }))).toBe('zzq mine')
  })

  it('falls back to the name for a built-in id it does not know', () => {
    expect(presetDisplayName(preset({ id: 'gone', name: 'zzq legacy', builtIn: true }))).toBe(
      'zzq legacy',
    )
  })
})
