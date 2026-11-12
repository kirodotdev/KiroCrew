import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

const SRC = path.resolve(__dirname, '..')
const PANEL = 'pages.settings.securityPanel'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('the governed ruleset rule count on Settings > Security', () => {
  it('keeps the English label', () => {
    expect(i18nT(`${PANEL}.block_list_rules`, { rules: i18nT(`${PANEL}.rule_count`, { count: 2 }) }))
      .toBe('Block-list · 2 rules')
    expect(i18nT(`${PANEL}.allow_list_rules`, { rules: i18nT(`${PANEL}.rule_count`, { count: 1 }) }))
      .toBe('Allow-list · 1 rule')
  })

  it('translates the count in German', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT(`${PANEL}.rule_count`, { count: 1 })).toBe('1 Regel')
    expect(i18nT(`${PANEL}.block_list_rules`, { rules: i18nT(`${PANEL}.rule_count`, { count: 3 }) }))
      .toBe('Verbotsliste · 3 Regeln')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(`${PANEL}.rule_count`, { count: 1 })).toBe('1 правило')
    expect(i18nT(`${PANEL}.rule_count`, { count: 3 })).toBe('3 правила')
    expect(i18nT(`${PANEL}.rule_count`, { count: 5 })).toBe('5 правил')
    expect(i18nT(`${PANEL}.rule_count`, { count: 21 })).toBe('21 правило')
  })

  it('no longer picks the English noun in the panel source', () => {
    const src = fs.readFileSync(path.join(SRC, 'pages/settings/SecurityPanelGovernance.tsx'), 'utf8')
    expect(src).not.toContain("'rule' : 'rules'")
    expect(src).toContain(`i18nT('${PANEL}.rule_count', { count: n })`)
  })
})
