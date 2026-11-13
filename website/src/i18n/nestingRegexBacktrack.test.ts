/**
 * Stored text that reaches i18next must not hang the tab.
 *
 * The dashboard passes some stored text (tool-step titles, question-card
 * options, reminders, command-bar queries) through `t()`. i18next scans every
 * key and translated value for its nesting marker with one regex. Before
 * i18next 26.4.4 that regex nested a `+` inside a `*`, so the marker followed
 * by unbalanced quotes or parentheses backtracks exponentially: each extra
 * character about doubles the time, and a title of a few dozen characters
 * freezes the tab for good. 26.4.4 changed the inner alternative to a single
 * character, which makes the scan linear.
 *
 * The behavior tests below run only once the installed version is at or above
 * the floor. On a regressed install they would hang the worker instead of
 * failing, and a synchronous hang cannot be cut short by a test timeout, so the
 * version floor is the test that turns red.
 *
 * The marker is built from char codes so this file never contains it
 * literally: an agent that greps or edits this file must not paste the marker
 * into a shell command, because the dashboard renders commands as titles.
 */
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'

const MARKER = String.fromCharCode(36) + 't('
const FLOOR = [26, 4, 4] as const

function installedVersion(): number[] {
  const require = createRequire(import.meta.url)
  const pkg = JSON.parse(readFileSync(require.resolve('i18next/package.json'), 'utf8')) as { version: string }
  return pkg.version.split('.').map((part) => Number.parseInt(part, 10))
}

function atLeastFloor(version: number[]): boolean {
  for (let i = 0; i < FLOOR.length; i += 1) {
    const have = version[i] ?? 0
    if (have !== FLOOR[i]) return have > FLOOR[i]
  }
  return true
}

const floorOk = atLeastFloor(installedVersion())

describe('i18next nesting regex', () => {
  it('is installed at or above the version with the linear nesting scan', () => {
    expect(floorOk, `installed i18next ${installedVersion().join('.')} is below ${FLOOR.join('.')}`).toBe(true)
  })

  it.runIf(floorOk)('returns an unclosed marker used as a key unchanged', () => {
    for (const tail of ['"', "'", '(', '"(', "'a(b"]) {
      const key = `${MARKER}${'a'.repeat(200)}${tail}`
      expect(i18next.t(key)).toBe(key)
    }
  })

  it.runIf(floorOk)('interpolates an unclosed marker as plain text', () => {
    const key = 'test.nestingRegexBacktrack.title'
    i18next.addResource('en', 'translation', key, 'Step: {{title}}')
    const title = `grep -lF '${MARKER}${'a'.repeat(200)}" x.jsonl`
    expect(i18next.t(key, { title })).toBe(`Step: ${title}`)
  })

  it.runIf(floorOk)('still resolves real nesting', () => {
    i18next.addResource('en', 'translation', 'test.nestingRegexBacktrack.inner', 'inner')
    i18next.addResource('en', 'translation', 'test.nestingRegexBacktrack.outer', `a ${MARKER}test.nestingRegexBacktrack.inner) b`)
    expect(i18next.t('test.nestingRegexBacktrack.outer')).toBe('a inner b')
  })
})
