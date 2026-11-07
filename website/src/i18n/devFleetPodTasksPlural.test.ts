import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

const KEY = 'pages.devFleetPage.pod_task_chip'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('the Dev Fleet pod tasks chip counts its tasks', () => {
  it('picks the English singular and plural forms', () => {
    expect(i18nT(KEY, { count: 1, n: '1' })).toBe('1 task')
    expect(i18nT(KEY, { count: 1234, n: '1,234' })).toBe('1,234 tasks')
  })

  it('agrees the Russian noun with the number', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(KEY, { count: 1, n: '1' })).toBe('1 задача')
    expect(i18nT(KEY, { count: 3, n: '3' })).toBe('3 задачи')
    expect(i18nT(KEY, { count: 5, n: '5' })).toBe('5 задач')
    expect(i18nT(KEY, { count: 21, n: '21' })).toBe('21 задача')
  })

  it('uses the singular in German and French for one task', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT(KEY, { count: 1, n: '1' })).toBe('1 Task')
    await i18next.changeLanguage('fr')
    expect(i18nT(KEY, { count: 1, n: '1' })).toBe('1 tâche')
    expect(i18nT(KEY, { count: 2, n: '2' })).toBe('2 tâches')
  })

  it('no longer glues the number to a fixed label in the row', () => {
    const lines = fs.readFileSync(path.resolve(__dirname, '../apps/dev-fleet/DevFleetPageRow.tsx'), 'utf8').split('\n')
    expect(lines.filter((line) => line.includes("'pages.devFleetPage.pod_tasks_label'"))).toEqual([])
    expect(lines.filter((line) => line.includes(`'${KEY}', { count: r.tasks, n: fmtNumber(r.tasks) }`))).toHaveLength(1)
  })
})
