/**
 * The scoping picker's fallback group name and its flow basis tags under a
 * non-English language.
 *
 * A screen with no group was listed under the English `screens`, and a
 * suggested flow carried an English `from links` or `guess` tag. These mount
 * the real picker in Russian and read what a user would read; each expected
 * value comes from the catalog and is checked against English first, so a
 * missing key cannot pass by falling back.
 */
import fs from 'node:fs'
import path from 'node:path'
import { afterAll, afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../../i18n/all'
import ScopingPicker from './ScopingPicker'
import type { Scope } from './types'

const K = 'apps.designCritique.scopingPicker.'
const SRC = path.resolve(__dirname, '..', '..')
const noop = () => {}

/** The `lng` value of `key`, guarded so a missing translation fails here. */
function tr(lng: string, key: string): string {
  const en = i18next.getFixedT('en')(K + key) as string
  const value = i18next.getFixedT(lng)(K + key) as string
  expect(value).not.toBe(en)
  expect(value).not.toBe(K + key)
  return value
}

function picker() {
  const scope = {
    framework: '',
    note: '',
    flows: [
      { label: 'Checkout', screenIds: ['a', 'b'], basis: 'observed' },
      { label: 'Signup', screenIds: ['b'], basis: 'inferred' },
    ],
    screens: [
      { id: 'a', label: 'Home', canSee: true },
      { id: 'b', label: 'Cart', canSee: true, group: 'shop' },
    ],
  } as unknown as Scope
  return render(
    <ScopingPicker scope={scope} picked={[]} refBrief="" dragId={null} togglePick={noop} dropPickAt={noop}
      movePick={noop} useFlow={noop} setDragId={noop} setRefBrief={noop} runScoped={noop} onStartOver={noop} />,
  )
}

afterEach(async () => {
  cleanup()
  await i18next.changeLanguage('en')
})
afterAll(async () => { await i18next.changeLanguage('en') })

describe('Design Critique scoping picker group and tags', () => {
  it('names the ungrouped screens and tags each flow in Russian', async () => {
    await i18next.changeLanguage('ru')
    picker()
    // The section heading reads the same word; the ungrouped group header is the second.
    const word = tr('ru', 'group_fallback')
    expect(screen.getAllByText((t) => t === word || t.startsWith(word))).toHaveLength(2)
    expect(screen.queryByText('screens')).toBeNull()
    expect(screen.getByText('shop')).toBeInTheDocument()
    expect(screen.getByText(tr('ru', 'tag_from_links'))).toBeInTheDocument()
    expect(screen.getByText(tr('ru', 'tag_guess'))).toBeInTheDocument()
  })

  it('has the three lines in each shipped language', () => {
    for (const lng of ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']) {
      for (const key of ['group_fallback', 'tag_from_links', 'tag_guess']) tr(lng, key)
    }
  })

  it.each(["s.group || 'screens'", "'from links'", ": 'guess'"])(
    'ScopingPicker.tsx takes its text from the catalog (%s)', (literal) => {
      const src = fs.readFileSync(path.join(SRC, 'apps/design-critique/ScopingPicker.tsx'), 'utf8')
      expect(src).not.toContain(literal)
    })
})
