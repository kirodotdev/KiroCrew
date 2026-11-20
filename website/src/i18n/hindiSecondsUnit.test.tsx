/**
 * Hindi writes the narrow seconds unit as `से`, the same word as the postposition
 * `से` ("since", "from"). A template that puts a duration right before that
 * postposition renders the word twice: the Overview uptime line read
 * `1 मि 4 से से चालू` (#36385). Each line below gets a duration that ends in
 * seconds, built by the formatter its caller uses.
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { Trans } from 'react-i18next'

import { i18next } from './all'
import { fmtDuration, fmtElapsed, fmtUnit } from './format'
import { i18nT } from './t'
import { fmtAge } from '../pages/system/TasksCapacityCard'

beforeEach(async () => {
  await i18next.changeLanguage('hi')
})

afterEach(async () => {
  await i18next.changeLanguage('en')
})

function uptimeLine(i18nKey: string): string {
  // The same parts `useUptime` passes for an uptime under an hour.
  const uptime = fmtDuration([[1, 'minute'], [4, 'second']])
  const { container } = render(
    <Trans i18nKey={i18nKey} values={{ version: '0.12.2' }} components={{ uptime: <span>{uptime}</span> }} />,
  )
  return container.textContent ?? ''
}

/**
 * Each place where the word से follows the word से. Brackets and punctuation
 * around a word are trimmed, so `3,600 से) से` counts; `से सेव` ("saved") does not.
 */
function doubledSe(text: string): string[] {
  const words = text.split(/\s+/).map(w => w.replace(/^\(+|[)।·:,]+$/g, ''))
  return words.flatMap((word, i) => (word === 'से' && words[i + 1] === 'से' ? [words.slice(Math.max(0, i - 2), i + 2).join(' ')] : []))
}

describe('Hindi lines with a duration in seconds (#36385)', () => {
  it.each([
    ['pages.overviewPage.uptime_line', () => uptimeLine('pages.overviewPage.uptime_line')],
    ['pages.overviewPage.uptime_line_version', () => uptimeLine('pages.overviewPage.uptime_line_version')],
    ['pages.chat.chatFooter.stall_elapsed', () => i18nT('pages.chat.chatFooter.stall_elapsed', { elapsed: fmtElapsed(38_000, { wholeSeconds: true }) })],
    ['pages.tasksCapacityCard.stalled_tip', () => i18nT('pages.tasksCapacityCard.stalled_tip', { age: fmtAge(64) })],
    ['pages.tasksCapacityCard.age_for', () => i18nT('pages.tasksCapacityCard.age_for', { age: fmtAge(64) })],
    ['pages.settings.chatPanel.compaction_wait_budget_clamped', () => i18nT('pages.settings.chatPanel.compaction_wait_budget_clamped', {
      requested: fmtUnit(5, 'second'),
      stored: fmtUnit(60, 'second'),
      min: fmtUnit(60, 'second'),
      max: fmtUnit(3600, 'second'),
    })],
  ])('%s never puts the postposition से right after the seconds unit', (_key, line) => {
    const text = line()
    expect(doubledSe(text), text).toEqual([])
  })
})
