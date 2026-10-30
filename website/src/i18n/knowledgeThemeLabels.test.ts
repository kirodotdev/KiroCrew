import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { fmtNumber } from './format'
import { i18nT } from './t'
import { itemStatusLabel, itemTypeLabel, ITEM_TYPES } from '../pages/knowledge/helpers'

const SRC = path.resolve(__dirname, '..')
const shared = (count: number) => i18nT('pages.knowledge.detailView.shared_count', { count, n: fmtNumber(count) })

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('knowledge item labels and the theme top bar frame title', () => {
  it('keeps the English text', () => {
    expect(itemTypeLabel('design_doc')).toBe('design doc')
    expect(itemTypeLabel('external_reference')).toBe('external reference')
    expect(itemStatusLabel('archived')).toBe('Archived')
    expect(shared(3)).toBe('3 shared')
    expect(i18nT('components.themeExperienceLayer.theme_topbar_dark')).toBe('theme top bar, dark mode')
  })

  it('translates every item type and both statuses', async () => {
    await i18next.changeLanguage('de')
    for (const t of ITEM_TYPES) expect(itemTypeLabel(t)).not.toBe(t.replace(/_/g, ' '))
    expect(itemTypeLabel('meeting_notes')).toBe('Besprechungsnotizen')
    expect(itemStatusLabel('active')).toBe('Aktiv')
    await i18next.changeLanguage('es')
    expect(itemStatusLabel('archived')).toBe('Archivado')
    await i18next.changeLanguage('de')
    expect(i18nT('components.themeExperienceLayer.theme_topbar_light')).toBe('Theme-Kopfleiste, heller Modus')
  })

  it('shows an unknown type and status as they come', () => {
    expect(itemTypeLabel('custom_kind')).toBe('custom kind')
    expect(itemStatusLabel('draft')).toBe('draft')
    expect(itemTypeLabel('toString')).toBe('toString')
    expect(itemStatusLabel('__proto__')).toBe('__proto__')
  })

  it('puts the shared count inside the Japanese and Bengali strings', async () => {
    await i18next.changeLanguage('ja')
    expect(shared(2)).toBe('共通 2 件')
    await i18next.changeLanguage('bn')
    expect(shared(12)).toBe('১২টি শেয়ার করা')
  })

  it('gives the busiest day, filtered mates and trend starts a singular form', async () => {
    const at = (count: number) => ({ count, n: fmtNumber(count) })
    expect(i18nT('apps.mochi.stats.busiest_day', { date: 'Mon', ...at(1) })).toBe('Chattiest day: Mon (1 msg)')
    expect(i18nT('apps.mochi.stats.busiest_day', { date: 'Mon', ...at(1200) })).toBe('Chattiest day: Mon (1,200 msgs)')
    expect(i18nT('pages.membersPage.member_count_filtered', { shown: fmtNumber(1), ...at(1) })).toBe('1 of 1 Bot')
    expect(i18nT('pages.telemetryPanel.daily_trend_point', { date: 'Mon', cold: '1s', warm: '2s', ...at(1) }))
      .toBe('Mon · cold 1s · warm 2s · 1 start')
    await i18next.changeLanguage('ru')
    expect(i18nT('pages.membersPage.member_count_filtered', { shown: fmtNumber(2), ...at(5) }))
      .toBe('Bot: 2 из 5')
    await i18next.changeLanguage('fr')
    expect(i18nT('pages.membersPage.member_count_filtered', { shown: fmtNumber(1), ...at(3) }))
      .toBe('Bots\u202f: 1/3')
  })

  it.each([
    ['apps/mochi/MochiPage.tsx', 'count: fmtNumber(s.busiestDay.messages)'],
    ['apps/mochi/src/renderer/SettingsPanel.tsx', 'count: fmtNumber(stats.busiestDay.messages)'],
    ['pages/TelemetryPanel.tsx', 'count: fmtNumber(r.count)'],
    ['pages/members/MembersPageRoster.tsx', 'shown: sortedMembers.length,'],
  ])('%s passes a raw count and a formatted number (%s)', (file, old) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(old)
  })

  it.each([
    ['pages/knowledge/DetailView.tsx', "{r.shared_entities} {i18nT("],
    ['pages/knowledge/DetailView.tsx', "item.item_type.replace(/_/g, ' ')"],
    ['pages/knowledge/DetailView.tsx', '>{item.status}</span>'],
    ['pages/knowledge/ItemCard.tsx', "item.item_type.replace(/_/g, ' ')"],
    ['pages/knowledge/index.tsx', "ITEM_TYPES.map(t => t.replace(/_/g, ' '))"],
    ['components/ThemeExperienceLayer.tsx', 'title={`theme topbar ('],
  ])('%s routes the label through the catalog (%s)', (file, raw) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(raw)
  })
})
