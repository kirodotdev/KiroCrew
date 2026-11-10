import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

// The Create Folders From Project disclosure counts its nested rows. Its key
// had no plural forms, so one row read "1 possible sub-folders" in English and
// the noun never agreed with the number in any language.
const KEY = 'apps.projectScaffolder.projectScaffolderPage.n_possible_sub_folders_inside_packages_above'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('nested sub-folder count picks the plural form for its number', () => {
  it('uses the English singular for one', () => {
    expect(i18nT(KEY, { count: 1, n: 1 })).toBe('1 possible sub-folder inside packages above')
    expect(i18nT(KEY, { count: 5, n: 5 })).toBe('5 possible sub-folders inside packages above')
  })

  it('agrees the Russian noun with the number', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(KEY, { count: 1, n: 1 })).toBe('1 возможная вложенная папка в пакетах выше')
    expect(i18nT(KEY, { count: 3, n: 3 })).toBe('3 возможные вложенные папки в пакетах выше')
    expect(i18nT(KEY, { count: 5, n: 5 })).toBe('5 возможных вложенных папок в пакетах выше')
  })

  it('uses the singular in German, Spanish and French', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT(KEY, { count: 1, n: 1 })).toBe('1 möglicher Unterordner in den Paketen oben')
    await i18next.changeLanguage('es')
    expect(i18nT(KEY, { count: 1, n: 1 })).toBe('1 posible subcarpeta dentro de los paquetes de arriba')
    await i18next.changeLanguage('fr')
    expect(i18nT(KEY, { count: 1, n: 1 })).toBe('1 sous-dossier possible dans les paquets ci-dessus')
  })
})
