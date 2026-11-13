import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { act, screen } from '@testing-library/react'
import { i18next } from '../i18n/all'
import { renderWithProviders } from '../test/helpers'
import LocalStorageDebug from './LocalStorageDebug'

// A group's "clear" button names how many keys it deletes. The count comes from
// the catalog's plural forms, so the label reads right in each language and for
// a group of one.

beforeEach(() => {
  localStorage.clear()
})

afterEach(async () => {
  localStorage.clear()
  await act(async () => { await i18next.changeLanguage('en') })
})

function seed(prefix: string, count: number) {
  for (let i = 0; i < count; i++) localStorage.setItem(`${prefix}${i}`, 'x')
}

async function clearTitles(language: string): Promise<string[]> {
  await act(async () => { await i18next.changeLanguage(language) })
  renderWithProviders(<LocalStorageDebug />)
  const clear = i18next.t('pages.localStorageDebug.clear')
  return screen.getAllByRole('button', { name: clear }).map(b => b.getAttribute('title') ?? '')
}

describe('LocalStorageDebug group delete label', () => {
  it('uses the plural form for the group size', async () => {
    seed('sort:', 3)
    seed('mc-chat-', 1)
    expect((await clearTitles('en')).sort()).toEqual(['Delete 1 key', 'Delete all 3 keys'])
  })

  it('is translated', async () => {
    seed('sort:', 2)
    expect(await clearTitles('de')).toEqual(['Alle 2 Schlüssel löschen'])
  })

  it('takes the many form where a language has one', async () => {
    seed('sort:', 5)
    expect(await clearTitles('ru')).toEqual(['Удалить все 5 ключей'])
  })
})
