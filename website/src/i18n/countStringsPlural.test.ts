import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

const SRC = path.resolve(__dirname, '..')

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('count strings on search, install, delete and import surfaces', () => {
  it('picks the English singular and plural forms', () => {
    expect(i18nT('components.searchBar.result_position', { current: 1, count: 1 })).toBe('1 of 1 result')
    expect(i18nT('components.searchBar.result_position', { current: 3, count: 5 })).toBe('3 of 5 results')
    expect(i18nT('components.markdownPanel.match_position', { current: 2, total: 7 })).toBe('2 of 7')
    expect(i18nT('components.skillBrowserModal.installed_files', { count: 4 })).toBe('Installed 4 files')
    expect(i18nT('pages.knowledge.index.delete_items_permanently', { count: 1 })).toBe('Delete 1 item permanently?')
    expect(i18nT('pages.knowledge.index.delete_items_permanently', { count: 3 })).toBe('Delete 3 items permanently?')
    expect(i18nT('components.agentImportFlow.source_item_count', { name: 'Claude Code', count: 1 })).toBe('Claude Code, 1 item')
  })

  it('translates them in German', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT('components.searchBar.result_position', { current: 3, count: 5 })).toBe('3 von 5 Ergebnissen')
    expect(i18nT('components.markdownPanel.match_position', { current: 2, total: 7 })).toBe('2 von 7')
    expect(i18nT('components.skillBrowserModal.installed_files', { count: 4 })).toBe('4 Dateien installiert')
    expect(i18nT('pages.knowledge.index.delete_items_permanently', { count: 3 })).toBe('3 Elemente endgültig löschen?')
    expect(i18nT('components.agentImportFlow.source_item_count', { name: 'Claude Code', count: 1 })).toBe('Claude Code, 1 Element')
  })

  it('inflects the Hindi result noun from the total, not the position', async () => {
    await i18next.changeLanguage('hi')
    expect(i18nT('components.searchBar.result_position', { current: 1, count: 5 })).toBe('5 नतीजों में से 1')
    expect(i18nT('components.searchBar.result_position', { current: 1, count: 1 })).toBe('1 नतीजे में से 1')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    const key = 'pages.knowledge.index.delete_items_permanently'
    expect(i18nT(key, { count: 1 })).toBe('Удалить 1 элемент навсегда?')
    expect(i18nT(key, { count: 3 })).toBe('Удалить 3 элемента навсегда?')
    expect(i18nT(key, { count: 5 })).toBe('Удалить 5 элементов навсегда?')
    expect(i18nT('components.skillBrowserModal.installed_files', { count: 21 })).toBe('Установлен 21 файл')
  })

  it.each([
    ['components/SearchBar.tsx', '} of ${matches.length} results`'],
    ['components/MarkdownPanelBody.tsx', '} of ${findCount}`'],
    ['components/SkillBrowserModal.tsx', '`Installed ${phase.fileCount} files`'],
    ['pages/knowledge/index.tsx', 'items permanently?`'],
    ['components/AgentImportFlow.tsx', '${count} items`'],
  ])('%s builds the count through the catalog, not an English template', (file, literal) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(literal)
  })
})
