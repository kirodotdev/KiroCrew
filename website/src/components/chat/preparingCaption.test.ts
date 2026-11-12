import { describe, it, expect, vi, afterEach } from 'vitest'

const lang = vi.hoisted(() => ({ current: 'en' }))
vi.mock('../../i18n', () => ({ i18next: { get language() { return lang.current } } }))
vi.mock('../../i18n/t', () => ({ i18nT: () => 'GENERIC' }))

import { preparingCaption } from './preparingCaption'

const slot = (text: string, preparingLang: string) => ({ preparing_text: text, preparing_lang: preparingLang })

describe('preparingCaption', () => {
  afterEach(() => { lang.current = 'en' })

  it("shows the gateway's caption when it is in the UI's language", () => {
    expect(preparingCaption(slot('Checking 2 PRs', 'en'))).toBe('Checking 2 PRs')
    lang.current = 'zh-CN'
    expect(preparingCaption(slot('正在查看', 'zh'))).toBe('正在查看')
  })

  it("keeps the UI's translated caption when the gateway wrote another language", () => {
    // A German UI never gets an English sentence in place of its own text.
    lang.current = 'de'
    expect(preparingCaption(slot('Checking 1 task', 'en'))).toBe('GENERIC')
  })

  it('falls back when the gateway sent no caption or no language', () => {
    expect(preparingCaption(undefined)).toBe('GENERIC')
    expect(preparingCaption(slot('', ''))).toBe('GENERIC')
    expect(preparingCaption(slot('Checking 1 task', ''))).toBe('GENERIC')
  })
})
