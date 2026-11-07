import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

// #32176: these counts used to print a raw number next to a fixed plural label
// ("1 rows", "1 keys"). Each is now one plural key with the number inside it.
const ROWS = 'pages.devFleetPage.rows_count'
const ROWS_FILTERED = 'pages.devFleetPage.rows_filtered_count'
const KEYS = 'pages.localStorageDebug.keys_count'
const OVERFLOW = 'pages.localStorageDebug.showing_200_of_count'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('count labels pick the plural form for their number (#32176)', () => {
  it('uses the English singular for one and keeps the plural text', () => {
    expect(i18nT(ROWS, { count: 1, n: '1' })).toBe('1 row')
    expect(i18nT(ROWS, { count: 1234, n: '1,234' })).toBe('1,234 rows')
    expect(i18nT(ROWS_FILTERED, { count: 4, shown: '2', n: '4' })).toBe('2 / 4 rows')
    expect(i18nT(KEYS, { count: 1, n: '1' })).toBe('1 key')
    expect(i18nT(KEYS, { count: 3, n: '3' })).toBe('3 keys')
    expect(i18nT(OVERFLOW, { count: 205, n: '205' })).toBe('Showing 200 of 205 — use filter to narrow')
  })

  it('agrees the Russian noun with the number', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(ROWS, { count: 1, n: '1' })).toBe('1 строка')
    expect(i18nT(ROWS, { count: 3, n: '3' })).toBe('3 строки')
    expect(i18nT(ROWS, { count: 5, n: '5' })).toBe('5 строк')
    expect(i18nT(ROWS, { count: 21, n: '21' })).toBe('21 строка')
    expect(i18nT(KEYS, { count: 1, n: '1' })).toBe('1 ключ')
    expect(i18nT(KEYS, { count: 3, n: '3' })).toBe('3 ключа')
    expect(i18nT(KEYS, { count: 5, n: '5' })).toBe('5 ключей')
  })

  it('no longer glues a number to the old fixed labels in either page', () => {
    const read = (rel: string) => fs.readFileSync(path.resolve(__dirname, rel), 'utf8')
    const fleet = read('../apps/dev-fleet/DevFleetPage.tsx')
    const storage = read('../pages/LocalStorageDebug.tsx')
    expect(fleet).not.toContain("'pages.devFleetPage.rows'")
    for (const old of ["'pages.localStorageDebug.keys'", 'showing_200_of\')', 'use_filter_to_narrow']) {
      expect(storage).not.toContain(old)
    }
  })
})
