import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'


let n = 0
let KEY = ''
beforeEach(() => { n += 1; KEY = `test.i18nTCache.k${n}` })
afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('i18nT plain-call cache', () => {
  it('returns the same string on repeat calls without re-reading i18next', () => {
    i18next.addResource('en', 'translation', KEY, 'first')
    expect(i18nT(KEY)).toBe('first')
    // A silent write changes the catalog without an event, so a second call
    // that still reads 'first' proves the answer came from the cache.
    i18next.addResource('en', 'translation', KEY, 'silently changed', { silent: true })
    expect(i18nT(KEY)).toBe('first')
  })

  it('drops the cache when a catalog is added after first use', () => {
    i18next.addResource('en', 'translation', KEY, 'before')
    expect(i18nT(KEY)).toBe('before')
    i18next.addResourceBundle('en', 'translation', { test: { i18nTCache: { [`k${n}`]: 'after' } } }, true, true)
    expect(i18nT(KEY)).toBe('after')
  })

  it('answers in the new language after a switch', async () => {
    i18next.addResource('en', 'translation', KEY, 'hello')
    expect(i18nT(KEY)).toBe('hello')
    i18next.addResourceBundle('fr', 'translation', { test: { i18nTCache: { [`k${n}`]: 'bonjour' } } }, true, true)
    await i18next.changeLanguage('fr')
    expect(i18nT(KEY)).toBe('bonjour')
    await i18next.changeLanguage('en')
    expect(i18nT(KEY)).toBe('hello')
  })

  it('never caches a call with vars', () => {
    i18next.addResource('en', 'translation', KEY, 'n={{n}}')
    expect(i18nT(KEY, { n: 1 })).toBe('n=1')
    expect(i18nT(KEY, { n: 2 })).toBe('n=2')
  })
})
