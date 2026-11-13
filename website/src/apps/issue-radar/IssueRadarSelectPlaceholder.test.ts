import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from '../../i18n/index'
import '../../i18n/all'
import { i18nT } from '../../i18n/t'

const KEY = 'apps.issueRadar.workspace.select_subject_to_see_details'
const CREW_KEY = 'apps.issueRadar.workspace.select_crew_to_see_details'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('the Issue Radar select-to-see-details placeholder', () => {
  it('keeps the English sentence for a pull request', () => {
    expect(i18nT(KEY, { subject: 'Pull Request' })).toBe('Select a Pull Request to see its details.')
    expect(i18nT(KEY, { subject: 'Merge Request' })).toBe('Select a Merge Request to see its details.')
  })

  it('places the noun where each language needs it', async () => {
    await i18next.changeLanguage('ja')
    expect(i18nT(KEY, { subject: 'Pull Request' })).toBe('Pull Request を選択すると詳細が表示されます。')
    await i18next.changeLanguage('ko')
    expect(i18nT(KEY, { subject: 'Pull Request' })).toBe('세부 정보를 보려면 Pull Request을(를) 선택하세요.')
    await i18next.changeLanguage('hi')
    expect(i18nT(KEY, { subject: 'Pull Request' })).toBe('विवरण देखने के लिए एक Pull Request चुनो।')
    await i18next.changeLanguage('de')
    expect(i18nT(KEY, { subject: 'Pull Request' })).toBe('Wähle einen Pull Request aus, um die Details zu sehen.')
  })

  it('gives the crew pane its own sentence so the article agrees with the noun', async () => {
    expect(i18nT(CREW_KEY)).toBe('Select a Crew to see its details.')
    await i18next.changeLanguage('fr')
    expect(i18nT(CREW_KEY)).toBe('Sélectionne une équipe pour voir ses détails.')
    await i18next.changeLanguage('de')
    expect(i18nT(CREW_KEY)).toBe('Wähle eine Crew aus, um die Details zu sehen.')
  })

  it('no longer glues the noun between two catalog fragments in Workspace.tsx', () => {
    const lines = fs.readFileSync(path.resolve(__dirname, 'Workspace.tsx'), 'utf8').split('\n')
    const glued = lines.filter((line) => line.includes("'apps.issueRadar.workspace.select_a'") || line.includes("'apps.issueRadar.workspace.to_see_its_details'"))
    expect(glued).toEqual([])
    expect(lines.filter((line) => line.includes(`'${KEY}'`))).toHaveLength(1)
    expect(lines.filter((line) => line.includes(`'${CREW_KEY}'`))).toHaveLength(1)
  })
})
