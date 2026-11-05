import { describe, it, expect, afterEach, vi } from 'vitest'
import { i18next } from '../../i18n/all'
import { formatRelativeDate } from './helpers'
import { sourceWordCountLabel } from './SourcesList'

// The Sources list row shows when a source last synced and how many words it
// holds. Both labels must follow the dashboard language: "today" / "yesterday"
// and "words" were English literals in every locale, and the word count picked
// its plural in JS, so "1 words" and Russian's three count forms were wrong.
describe('Sources row labels follow the dashboard language', () => {
  afterEach(async () => {
    vi.useRealTimers()
    await i18next.changeLanguage('en')
  })

  const viewAt = (now: Date) => {
    vi.useFakeTimers()
    vi.setSystemTime(now)
  }

  it('says today and yesterday in the active language', async () => {
    await i18next.changeLanguage('de')
    viewAt(new Date(2026, 9, 26, 0, 30, 0))
    expect(formatRelativeDate(new Date(2026, 9, 26, 0, 10, 0).toISOString())).toBe('heute')
    expect(formatRelativeDate(new Date(2026, 9, 25, 23, 0, 0).toISOString())).toBe('gestern')
  })

  it('keeps English today and yesterday in English', () => {
    viewAt(new Date(2026, 9, 26, 0, 30, 0))
    expect(formatRelativeDate(new Date(2026, 9, 26, 0, 10, 0).toISOString())).toBe('today')
    expect(formatRelativeDate(new Date(2026, 9, 25, 23, 0, 0).toISOString())).toBe('yesterday')
  })

  it('picks the English singular for one word', () => {
    expect(sourceWordCountLabel(1)).toBe('1 word')
    expect(sourceWordCountLabel(42)).toBe('42 words')
  })

  it('shows a compact approximate count from 1000 words', () => {
    expect(sourceWordCountLabel(12_400)).toBe('~12.4K words')
  })

  it('uses each Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    expect(sourceWordCountLabel(1)).toBe('1 слово')
    expect(sourceWordCountLabel(3)).toBe('3 слова')
    expect(sourceWordCountLabel(5)).toBe('5 слов')
  })

  it('translates the word count into another language', async () => {
    await i18next.changeLanguage('ja')
    expect(sourceWordCountLabel(42)).toBe('42 語')
  })
})
