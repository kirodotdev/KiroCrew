/**
 * A flow finding's step range and the empty screen-label fallback under a
 * non-English language.
 *
 * `FindingRow` glued `· steps ` to the range, and `shortLabel` returned the
 * English word `Screen` for an empty label, beside translated text. These read
 * what a user would read in Russian; each expected value comes from the catalog
 * and is checked against English first, so a missing key cannot pass by falling
 * back.
 */
import fs from 'node:fs'
import path from 'node:path'
import { afterAll, afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../../i18n/all'
import FindingRow from './FindingRow'
import { shortLabel } from './utils'

const K = 'apps.designCritique.'
const SRC = path.resolve(__dirname, '..', '..')
const noop = () => {}

/** The `lng` value of `key`, guarded so a missing translation fails here. */
function tr(lng: string, key: string, opts?: Record<string, unknown>): string {
  const en = i18next.getFixedT('en')(K + key, opts) as string
  const value = i18next.getFixedT(lng)(K + key, opts) as string
  expect(value).not.toBe(en)
  expect(value).not.toBe(K + key)
  return value
}

afterEach(async () => {
  cleanup()
  await i18next.changeLanguage('en')
})
afterAll(async () => { await i18next.changeLanguage('en') })

describe('Design Critique step range and screen fallback', () => {
  it('shows a flow finding step range in Russian', async () => {
    await i18next.changeLanguage('ru')
    render(
      <FindingRow
        finding={{ severity: 'minor', title: 'zzz', category: 'Flow' }} index={0} isOpen={false} isActive={false}
        flowScoped pinNum={undefined} stepRange="2–4" withMarks={t => t}
        onEnter={noop} onLeave={noop} onClick={noop} rowRef={noop}
      />,
    )
    expect(screen.getByText('· шаги 2–4')).toBeInTheDocument()
  })

  it('shows a translated flow badge and no range line when a flow finding has no steps', async () => {
    await i18next.changeLanguage('ru')
    const { container } = render(
      <FindingRow
        finding={{ severity: 'minor', title: 'zzz', category: 'Flow' }} index={0} isOpen={false} isActive={false}
        flowScoped pinNum={undefined} stepRange="" withMarks={t => t}
        onEnter={noop} onLeave={noop} onClick={noop} rowRef={noop}
      />,
    )
    expect(screen.getByText(tr('ru', 'findingRow.flow_badge'))).toBeInTheDocument()
    expect(container.textContent).not.toContain(tr('ru', 'findingRow.steps_range', { range: '' }).trim())
    expect(container.textContent).not.toMatch(/\bflow\b/)
  })

  it('names an unlabeled screen in Russian', async () => {
    await i18next.changeLanguage('ru')
    expect(shortLabel('')).toBe('Экран')
    expect(shortLabel('  (empty state)  ')).toBe('Экран')
  })

  it('has both lines in each shipped language', () => {
    for (const lng of ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']) {
      tr(lng, 'findingRow.steps_range', { range: '2–4' })
      tr(lng, 'utils.screen_fallback')
      tr(lng, 'findingRow.flow_badge')
    }
  })

  it.each([
    ['apps/design-critique/FindingRow.tsx', "'· steps ' + stepRange"],
    ['apps/design-critique/utils.ts', "return 'Screen'"],
    ['apps/design-critique/DesignCritiquePage.tsx', "if (!st.length) return 'flow'"],
  ])('%s takes its text from the catalog (%s)', (file, literal) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(literal)
  })
})
