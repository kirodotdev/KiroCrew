/**
 * The accessory travels under ONE key, and every side agrees on it.
 *
 * This is a shape-agreement test, not a behaviour test, because the bug it guards
 * is invisible at runtime in the direction that matters: the gallery wrote
 * `kiro.accessory`, the overlay read `kiro.accessory`, and the gallery's own
 * picker read a FLAT `accessory` — so choosing a prop worked, and re-opening the
 * picker showed "none". Nothing threw, nothing failed a typecheck.
 *
 * A grep-shaped test is the right instrument here: the three sites live in
 * different files and only their agreement is the invariant.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const APP = join(__dirname, '..', 'apps', 'crew-companion')
const read = (f: string) => readFileSync(join(APP, f), 'utf8')

describe('the dress-up prop uses one config shape everywhere', () => {
  it('the gallery writes it nested under kiro', () => {
    expect(read('GalleryPanel.tsx')).toContain('kiro: { accessory:')
  })

  it('the gallery reads it back from the same place it wrote it', () => {
    const src = read('GalleryPanel.tsx')
    expect(src).toMatch(/kiro\?\.\s*accessory/)
    // and not from the flat key, which is what made the picker forget the choice
    expect(src).not.toMatch(/\bc\?\.accessory\b/)
  })

  it('the overlay reads the same nested key', () => {
    expect(read('pet.tsx')).toMatch(/kiro\?\.\s*accessory/)
  })
})

describe('config reads go to an endpoint that answers GET', () => {
  it('custom presets are read from the reminders snapshot, not the POST-only config path', () => {
    const src = read('petBridge.ts')
    // Assert on the CALL, not the surrounding prose: the comment above it names
    // CONFIG_PATH to explain why it is wrong, which a text search would count.
    const call = src.match(/presetsLoadCustom\(\)[\s\S]*?getJson<[^>]*>\((\w+)\)/)
    expect(call?.[1]).toBe('REMINDERS_PATH')
  })

  it('presets are still SAVED to the config path, which is the POST route', () => {
    const src = read('petBridge.ts')
    const saver = src.slice(src.indexOf('async presetsSaveCustom('))
    expect(saver.slice(0, saver.indexOf('},'))).toContain('CONFIG_PATH')
  })
})
