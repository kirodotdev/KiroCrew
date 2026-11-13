import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

const M = 'pages.mcpManagement'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('the pre-install server counts on Settings > MCP', () => {
  it('picks the English singular and plural forms', () => {
    expect(i18nT(`${M}.resolve_ready`, { count: 1 })).toBe('1 server pre-installed.')
    expect(i18nT(`${M}.resolve_ready`, { count: 3 })).toBe('3 servers pre-installed.')
    expect(i18nT(`${M}.resolve_all_failed`, { count: 1 })).toBe('Could not pre-install 1 server.')
    expect(i18nT(`${M}.resolve_all_failed`, { count: 2 })).toBe('Could not pre-install 2 servers.')
  })

  it('uses the French singular instead of serveur(s)', async () => {
    await i18next.changeLanguage('fr')
    expect(i18nT(`${M}.resolve_ready`, { count: 1 })).toBe('1 serveur préinstallé.')
    expect(i18nT(`${M}.resolve_ready`, { count: 4 })).toBe('4 serveurs préinstallés.')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(`${M}.resolve_all_failed`, { count: 1 })).toBe('Не удалось предустановить 1 сервер.')
    expect(i18nT(`${M}.resolve_all_failed`, { count: 3 })).toBe('Не удалось предустановить 3 сервера.')
    expect(i18nT(`${M}.resolve_all_failed`, { count: 5 })).toBe('Не удалось предустановить 5 серверов.')
    expect(i18nT(`${M}.resolve_ready`, { count: 21 })).toBe('Предустановлен 21 сервер.')
  })
})
