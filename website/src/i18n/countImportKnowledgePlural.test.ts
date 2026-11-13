import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

const SRC = path.resolve(__dirname, '..')
const AI = 'components.agentImportFlow'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('counts on the agent import review and the Knowledge selection bar', () => {
  it('picks the English singular and plural forms', () => {
    expect(i18nT(`${AI}.category_count`, { count: 1 })).toBe('1 category')
    expect(i18nT(`${AI}.category_count`, { count: 3 })).toBe('3 categories')
    expect(i18nT(`${AI}.source_categories`, { name: 'Claude Code' })).toBe('Claude Code categories')
    expect(i18nT(`${AI}.items_selected_count`, { count: 1 })).toBe('1 item selected')
    expect(i18nT('pages.knowledge.index.selected_count', { count: 4 })).toBe('4 selected')
    expect(i18nT(`${AI}.found_count`, { count: 12 })).toBe('12 found')
  })

  it('translates them in German', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT(`${AI}.category_count`, { count: 3 })).toBe('3 Kategorien')
    expect(i18nT(`${AI}.source_categories`, { name: 'Claude Code' })).toBe('Kategorien von Claude Code')
    expect(i18nT(`${AI}.items_selected_count`, { count: 1 })).toBe('1 Element ausgewählt')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    const key = `${AI}.category_count`
    expect(i18nT(key, { count: 21 })).toBe('21 категория')
    expect(i18nT(key, { count: 3 })).toBe('3 категории')
    expect(i18nT(key, { count: 5 })).toBe('5 категорий')
  })

  it.each([
    ['components/AgentImportFlow.tsx', "=== 1 ? 'category' : 'categories'"],
    ['components/AgentImportFlow.tsx', '${source.name} categories`'],
    ['components/AgentImportFlow.tsx', '{selectedItemCount} {i18nT('],
    ['components/AgentImportFlow.tsx', '{count} {i18nT('],
    ['pages/knowledge/index.tsx', '{selectedIds.size} {i18nT('],
  ])('%s builds the label through the catalog (%s)', (file, english) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(english)
  })
})
