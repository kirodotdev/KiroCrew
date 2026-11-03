import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { fmtNumber } from './format'
import { i18nT } from './t'

const SRC = path.resolve(__dirname, '..')
const DEPLOY = 'pages.artifactDeployPage.more_count'
const TASKS = 'pages.chat.taskProgressBar.more_count'
const EVENTS = 'pages.chat.workflowSidebarRow.events_count'
const at = (count: number) => ({ count, n: fmtNumber(count) })

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('overflow and event counts on deploy profiles, task list and workflow rows', () => {
  it('keeps the English text and picks the singular event', () => {
    expect(i18nT(DEPLOY, at(4))).toBe('+4 more')
    expect(i18nT(TASKS, at(2))).toBe('+ 2 more…')
    expect(i18nT(EVENTS, at(1))).toBe('1 event')
    expect(i18nT(EVENTS, at(9))).toBe('9 events')
  })

  it('uses every Russian plural form for events', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(EVENTS, at(1))).toBe('1 событие')
    expect(i18nT(EVENTS, at(3))).toBe('3 события')
    expect(i18nT(EVENTS, at(5))).toBe('5 событий')
    expect(i18nT(EVENTS, at(21))).toBe('21 событие')
    expect(i18nT(DEPLOY, at(4))).toBe('ещё 4')
  })

  it('reads "more" as a count, not "details", in Japanese', async () => {
    await i18next.changeLanguage('ja')
    expect(i18nT(DEPLOY, at(4))).toBe('ほか 4 件')
    expect(i18nT(TASKS, at(2))).toBe('ほか 2 件…')
  })

  it('writes the number in Bengali digits', async () => {
    await i18next.changeLanguage('bn')
    expect(i18nT(EVENTS, at(12))).toBe('১২টি ইভেন্ট')
  })

  it('agrees in Italian', async () => {
    await i18next.changeLanguage('it')
    expect(i18nT(DEPLOY, at(1))).toBe('+1 altro')
    expect(i18nT(DEPLOY, at(3))).toBe('+3 altri')
  })

  it.each([
    ['pages/ArtifactDeployPage.tsx', '{availableProfiles.length - 8} {i18nT('],
    ['pages/chat/TaskProgressBar.tsx', '{tasks.length - MAX_VISIBLE_ROWS} {i18nT('],
    ['pages/chat/WorkflowSidebarRow.tsx', '{row.event_count ?? 0} {i18nT('],
  ])('%s passes the count into the catalog (%s)', (file, glued) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(glued)
  })
})
