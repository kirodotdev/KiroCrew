import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { fmtNumber } from './format'
import { i18nT } from './t'
import { formatRecentWindow, RECENT_WINDOW_PRESETS } from '../pages/recentWindow'

const SRC = path.resolve(__dirname, '..')
const SB = 'pages.chatSidebar'
const at = (count: number) => ({ count, n: fmtNumber(count) })
const HOUR = 60 * 60 * 1000

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('sidebar count sentences and recency labels', () => {
  it('keeps the English text', () => {
    expect(i18nT(`${SB}.skip_running_sessions`, at(1))).toBe('Skip 1 running session')
    expect(i18nT(`${SB}.skip_running_sessions`, at(2))).toBe('Skip 2 running sessions')
    expect(i18nT(`${SB}.cleanup_move_count`, at(3))).toBe('3 sessions will be moved to older sessions.')
    expect(i18nT(`${SB}.show_sessions_count`, at(1))).toBe('Show 1 session')
    expect(i18nT(`${SB}.hide_sessions_count`, at(4))).toBe('Hide 4 sessions')
    expect(formatRecentWindow(HOUR)).toBe('1h')
    expect(formatRecentWindow(48 * HOUR)).toBe('2d')
    expect(formatRecentWindow(30 * 60 * 1000)).toBe('30m')
    expect(RECENT_WINDOW_PRESETS.map(p => p.label)).toEqual(['1 hour', '6 hours', '1 day', '1 week'])
  })

  it('agrees the German verb with one session', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT(`${SB}.cleanup_move_count`, at(1))).toBe('1 Sitzung wird zu älteren Sitzungen verschoben.')
    expect(i18nT(`${SB}.cleanup_move_count`, at(5))).toBe('5 Sitzungen werden zu älteren Sitzungen verschoben.')
    expect(i18nT(`${SB}.skip_running_sessions`, at(2))).toBe('2 laufende Sitzungen überspringen')
  })

  it('uses every Russian plural form', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(`${SB}.skip_running_sessions`, at(1))).toBe('Пропустить 1 активную сессию')
    expect(i18nT(`${SB}.skip_running_sessions`, at(3))).toBe('Пропустить 3 активные сессии')
    expect(i18nT(`${SB}.show_sessions_count`, at(5))).toBe('Показать 5 сессий')
  })

  it('reads the recency labels in the active language', async () => {
    await i18next.changeLanguage('ru')
    expect(formatRecentWindow(HOUR)).not.toBe('1h')
    expect(RECENT_WINDOW_PRESETS[0].label).not.toBe('1 hour')
    await i18next.changeLanguage('de')
    expect(RECENT_WINDOW_PRESETS[2].label).toBe('1 Tag')
  })

  it.each([
    ['pages/chat-sidebar/bulkModelSwitch.tsx', "{i18nT('pages.chatSidebar.skip')} {i18nT("],
    ['pages/chat-sidebar/sessionCleanup.tsx', "{i18nT('pages.chatSidebar.will_be_moved_to_older_sessions')}"],
    ['pages/chat-sidebar/sessionCleanup.tsx', "i18nT('pages.chatSidebar.show')} {i18nT("],
    ['pages/recentWindow.ts', "unit === 'days' ? 'd'"],
    ['pages/recentWindow.ts', "label: '1 hour'"],
  ])('%s builds the label from one key or formatter (%s)', (file, glued) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(glued)
  })
})
