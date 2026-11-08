/**
 * Copy guard for the desktop gateway auto-update context note (#23874).
 *
 * The note sits above the gateway auto-update switch on the About page when a
 * desktop window is connected to a gateway it did not install. A usability test
 * found that a reader who meets "gateway" as the first word of the note, before
 * learning what it does for them, tends to leave the switch alone. The note now
 * leads with the reader's agents and chats and introduces the word in passing.
 *
 * These checks pin that order in English and keep the facts and the placeholder
 * the note needs in every catalog that carries it.
 */

import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import enManual from './locales/en.manual.json'

const KEY = ['pages', 'settings', 'aboutPanel', 'gateway_auto_update_desktop_context']
const LOCALES_DIR = path.join(__dirname, 'locales')

const lookup = (catalog: unknown): unknown =>
  KEY.reduce<unknown>((node, part) => {
    if (node && typeof node === 'object') return (node as Record<string, unknown>)[part]
    return undefined
  }, catalog)

const english = lookup(enManual) as string

describe('gateway auto-update desktop context note', () => {
  it('leads with what the switch is for, not with the word "gateway"', () => {
    expect(typeof english).toBe('string')
    expect(english.startsWith('Your agents and chats')).toBe(true)
    const firstSentence = english.split('. ')[0]
    // "gateway" is introduced in the first sentence, after the reader's own things.
    expect(firstSentence.indexOf('gateway')).toBeGreaterThan(firstSentence.indexOf('chats'))
  })

  it('keeps the three facts the note gives', () => {
    // Separate from the app, updates the gateway rather than the app, and
    // restarts only when nothing is running.
    expect(english).toMatch(/installed separately from \{\{productName\}\}/)
    expect(english).toMatch(/not this app/)
    expect(english).toMatch(/only restarts when no work is active/)
  })

  it('is present, with the product placeholder, in every catalog that carries it', () => {
    const files = readdirSync(LOCALES_DIR).filter(f => f.endsWith('.json'))
    const carriers = files.filter(f => {
      const value = lookup(JSON.parse(readFileSync(path.join(LOCALES_DIR, f), 'utf-8')))
      return typeof value === 'string'
    })
    // en.manual.json plus the eleven translated catalogs and the pseudolocale.
    expect(carriers.sort()).toEqual([
      'bn.json', 'de.json', 'en-XA.json', 'en.manual.json', 'es.json', 'fr.json', 'hi.json',
      'it.json', 'ja.json', 'ko.json', 'pt.json', 'ru.json', 'zh-CN.json',
    ])
    for (const file of carriers) {
      const value = lookup(JSON.parse(readFileSync(path.join(LOCALES_DIR, file), 'utf-8'))) as string
      expect(value, file).toContain('{{productName}}')
    }
  })
})
