/**
 * A surface that cannot change a value must not write it.
 *
 * `nestConfig` spreads EVERY stored key into `config.mochi`, and the Settings
 * panel saves its whole config object — so a Settings save re-posted whichever
 * appearance was live when that panel LOADED. Applying a pack in the Avatars
 * window and then saving Settings silently reverted the pack: the write landed
 * and was then overwritten, so there was no error to see, and the pet fell back
 * to its built-in-cat branch, which never touches the pack machinery and
 * therefore logs nothing either. Two clean paths, one silent regression.
 */

import { describe, expect, it } from 'vitest'

import { flattenConfig, nestConfig } from '../src/mochiApi'

/** A stored settings object with an imported pack active. */
const STORED = {
  activeAppearance: '666c03d6-dc5b-47bb-84d2-56f9e2a923cb',
  catPreset: 'ginger',
  colorMaps: { 'default-mochi': { body: '#fff' } },
  customPresets: [{ id: 'mine' }],
  petName: 'Bao',
  mode: 'quiet',
  language: '',
} as unknown as Parameters<typeof nestConfig>[0]

describe('appearance identity is not clobbered by an unrelated save', () => {
  it('round-trips into the nested shape the vendored renderer reads', () => {
    const nested = nestConfig(STORED) as unknown as {
      mochi: Record<string, unknown>
    }
    // It MUST be present in the nested view — the gallery reads the live pack id
    // from here. The bug was never that it was missing; it was that it came back.
    expect(nested.mochi.activeAppearance).toBe('666c03d6-dc5b-47bb-84d2-56f9e2a923cb')
  })

  it('drops gallery-owned keys from a whole-config save', () => {
    const nested = nestConfig(STORED) as unknown as Parameters<typeof flattenConfig>[0]
    const patch = flattenConfig(nested) as Record<string, unknown>
    for (const key of [
      'activeAppearance',
      'catPreset',
      'colorMaps',
      'customPresets',
    ]) {
      expect(patch, `${key} must not ride along on an unrelated save`).not.toHaveProperty(key)
    }
  })

  it('still forwards the keys the Settings panel does own', () => {
    const nested = nestConfig(STORED) as unknown as Parameters<typeof flattenConfig>[0]
    const patch = flattenConfig(nested) as Record<string, unknown>
    // Guard the guard: if the exclusion grew too wide, saving Settings would stop
    // persisting the fields it actually edits — a quieter failure than the one
    // this file exists to prevent.
    expect(patch.petName).toBe('Bao')
    expect(patch.mode).toBe('quiet')
  })

  it('drops the keys the builtin does not own at all', () => {
    const patch = flattenConfig({
      mochi: { soul: 'x', theme: 'dark', petName: 'Bao' },
    } as unknown as Parameters<typeof flattenConfig>[0]) as Record<string, unknown>
    expect(patch).not.toHaveProperty('soul')
    expect(patch).not.toHaveProperty('theme')
    expect(patch.petName).toBe('Bao')
  })
})
