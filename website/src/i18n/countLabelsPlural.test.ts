import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { fmtNumber } from './format'
import { i18nT } from './t'

const SRC = path.resolve(__dirname, '..')
const SEC = 'pages.settings.securityPanel'
const at = (count: number) => ({ count, n: fmtNumber(count) })

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('counts on Worlds, knowledge sources, MCP tools and the deny list', () => {
  it('picks the English singular and plural forms', () => {
    expect(i18nT('pages.worldsPage.summary', {
      present: i18nT('pages.worldsPage.agents_present', at(1)),
      active: i18nT('pages.worldsPage.agents_active', at(1)),
      open: i18nT('pages.worldsPage.slots_open', at(7)),
    })).toBe('1 agent present · 1 active · 7 slots open')
    expect(i18nT('pages.knowledge.sourcesList.items_count', at(1))).toBe('1 item')
    expect(i18nT('pages.knowledge.sourcesList.items_count', at(4))).toBe('4 items')
    expect(i18nT('pages.overview.mcpTab.tools_count', at(1))).toBe('1 tool')
    expect(i18nT('pages.overview.mcpTab.tools_count', at(12))).toBe('12 tools')
    expect(i18nT(`${SEC}.rules_matched`, { matched: fmtNumber(2), ...at(1) })).toBe('2 / 1 rule')
    expect(i18nT(`${SEC}.rules_matched`, { matched: fmtNumber(1), ...at(2) })).toBe('1 / 2 rules')
    expect(i18nT(`${SEC}.categories_count`, at(1))).toBe('1 category')
    expect(i18nT(`${SEC}.rules_count`, at(30))).toBe('30 rules')
  })

  it('translates them in German', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT('pages.knowledge.sourcesList.items_count', at(1))).toBe('1 Element')
    expect(i18nT('pages.overview.mcpTab.tools_count', at(3))).toBe('3 Werkzeuge')
    expect(i18nT(`${SEC}.categories_count`, at(1))).toBe('1 Kategorie')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    const key = 'pages.overview.mcpTab.tools_count'
    expect(i18nT(key, at(1))).toBe('1 инструмент')
    expect(i18nT(key, at(3))).toBe('3 инструмента')
    expect(i18nT(key, at(5))).toBe('5 инструментов')
    expect(i18nT(key, at(21))).toBe('21 инструмент')
    expect(i18nT(`${SEC}.rules_matched`, { matched: fmtNumber(4), ...at(22) })).toBe('4 / 22 правила')
    expect(i18nT('pages.knowledge.sourcesList.items_count', at(11))).toBe('11 элементов')
  })

  it('writes the number in Bengali digits', async () => {
    await i18next.changeLanguage('bn')
    expect(i18nT('pages.overview.mcpTab.tools_count', at(12))).toBe('১২টি টুল')
  })

  it('puts the number where Japanese grammar wants it', async () => {
    await i18next.changeLanguage('ja')
    expect(i18nT('pages.overview.mcpTab.tools_count', at(2))).toBe('2 個のツール')
    expect(i18nT('pages.worldsPage.slots_open', at(3))).toBe('空きスロット 3 個')
  })

  it('renders the Worlds line in Chinese without a stray measure word', async () => {
    await i18next.changeLanguage('zh-CN')
    expect(i18nT('pages.worldsPage.summary', {
      present: i18nT('pages.worldsPage.agents_present', at(2)),
      active: i18nT('pages.worldsPage.agents_active', at(1)),
      open: i18nT('pages.worldsPage.slots_open', at(6)),
    })).toBe('2 个代理在场，1 个活动，6 个空闲槽位')
  })

  it.each([
    ['pages/WorldsPage.tsx', '{activeCount} {i18nT('],
    ['pages/WorldsPage.tsx', "{i18nT('pages.worldsPage.present')}"],
    ['pages/knowledge/SourcesList.tsx', '{f.item_count} {i18nT('],
    ['pages/knowledge/SourcesList.tsx', '{s.item_count ?? 0} {i18nT('],
    ['pages/overview/McpTab.tsx', '{s.tools.length} {i18nT('],
    ['pages/settings/SecurityPanelDeniedCommands.tsx', '{dc.builtins.length} {i18nT('],
    ['pages/settings/SecurityPanelDeniedCommands.tsx', '{Object.keys(grouped).length} {i18nT('],
  ])('%s passes the count into the catalog (%s)', (file, glued) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(glued)
  })
})
