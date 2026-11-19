/**
 * The import dialog's settings-document copy exists in every locale catalog.
 *
 * A Replace import keeps this install's config.json, config.local.json and hooks.json
 * unless the box is ticked. The dialog states which files are kept or overwritten, and a
 * second line warns that the archive is foreign. i18next renders a missing key as the
 * key string itself, so a locale without one of these would show a raw key in a dialog
 * that guards a destructive action.
 */

import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

const LOCALES_DIR = path.join(__dirname, 'locales')
const PREFIX = ['pages', 'overview', 'portabilityTab']
const KEYS = [
  'import_include_settings',
  'replace_keeps_settings',
  'replace_overwrites_settings',
  'replace_settings_foreign_archive',
  'settings_kept_name_hooks',
  'import_settings_installed',
  'replace_installs_missing_settings',
]

function lookup(doc: unknown, key: string): unknown {
  let node = doc as Record<string, unknown> | undefined
  for (const part of [...PREFIX, key]) {
    if (node === undefined || node === null || typeof node !== 'object') return undefined
    node = node[part] as Record<string, unknown> | undefined
  }
  return node
}

describe('portability settings-document copy', () => {
  // en.json is the generated English catalog; en.manual.json is its hand-written source.
  const files = readdirSync(LOCALES_DIR).filter(f => f.endsWith('.json') && f !== 'en.json')

  it('is carried by every locale catalog, whatever languages the directory holds', () => {
    expect(files.length).toBeGreaterThan(0)
    expect(files).toContain('en.manual.json')
    for (const key of KEYS) {
      const carriers = files.filter(f => {
        const value = lookup(JSON.parse(readFileSync(path.join(LOCALES_DIR, f), 'utf-8')), key)
        return typeof value === 'string' && value !== ''
      })
      expect(carriers.sort(), key).toEqual([...files].sort())
    }
  })

  it('names the files the English dialog text is about', () => {
    const en = JSON.parse(readFileSync(path.join(LOCALES_DIR, 'en.manual.json'), 'utf-8'))
    expect(lookup(en, 'replace_keeps_settings')).toContain('config.json')
    expect(lookup(en, 'replace_keeps_settings')).toContain('hooks.json')
    expect(lookup(en, 'replace_overwrites_settings')).toContain('config.json')
    expect(lookup(en, 'replace_overwrites_settings')).toContain('hooks.json')
    expect(lookup(en, 'replace_overwrites_settings')).toContain('disabled or paused')
  })

  it('names all three files on the import checkbox in every locale', () => {
    // en-XA is generated from English and rewrites every letter, so it is not checked by name.
    // en.json carries no copy of this key (the first test pins which files do).
    for (const f of files.filter(name => name !== 'en-XA.json' && name !== 'en.json')) {
      const doc = JSON.parse(readFileSync(path.join(LOCALES_DIR, f), 'utf-8'))
      const label = lookup(doc, 'import_include_settings')
      expect(label, f).toEqual(expect.stringContaining('config.local.json'))
      expect(label, f).toEqual(expect.stringContaining('hooks.json'))
    }
  })
})
