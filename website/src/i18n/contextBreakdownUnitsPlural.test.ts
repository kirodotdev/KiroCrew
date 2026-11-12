import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'
import { fmtNumber } from './format'

const SRC = path.resolve(__dirname, '..')
const CHARS = 'pages.contextBreakdown.n_chars'
const TOKENS = 'pages.contextBreakdown.n_tokens'
const size = (key: string, count: number) => i18nT(key, { count, n: fmtNumber(count) })

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('context breakdown sizes carry their count inside one plural key', () => {
  it('reads the English singular and plural forms', () => {
    expect(size(CHARS, 1)).toBe('1 character')
    expect(size(CHARS, 12345)).toBe('12,345 characters')
    expect(size(TOKENS, 1)).toBe('1 token')
    expect(size(TOKENS, 85312)).toBe('85,312 tokens')
  })

  it('translates them in German and uses every Russian form', async () => {
    await i18next.changeLanguage('de')
    expect(size(TOKENS, 1)).toBe('1 Token')
    expect(size(CHARS, 3)).toBe('3 Zeichen')
    await i18next.changeLanguage('ru')
    expect(size(CHARS, 1)).toBe('1 символ')
    expect(size(CHARS, 3)).toBe('3 символа')
    expect(size(TOKENS, 5)).toBe('5 токенов')
    expect(size(TOKENS, 21)).toBe('21 токен')
  })

  it.each(['pages/ContextBreakdownPanel.tsx', 'pages/ContextEngineBreakdown.tsx'])(
    '%s no longer glues a number to a single-form unit word',
    (file) => {
      const lines = fs.readFileSync(path.join(SRC, file), 'utf8').split('\n')
      const glued = lines.filter(
        (line) => line.includes('fmtN(') && (line.includes("i18nT('pages.contextBreakdown.unit_chars')") || line.includes("i18nT('pages.contextBreakdown.unit_tokens')")),
      )
      expect(glued).toEqual([])
    },
  )
})
