/**
 * The bug, rendered.
 *
 * Design Critique says the target kind back to the user before they start
 * ("Figma file - I'll pull the frames"). The bold noun in that line was read
 * from KIND_LABEL, a raw-English map in constants.ts, while the surrounding
 * surface is translated. No i18n gate can see it: the strings are values in an
 * object literal, not JSX text or a translatable prop, and the catalog keys that
 * hold the translations already exist, so a key-presence check passes too.
 *
 * The translations were already written by native speakers, in 11 catalogs, and
 * never reached a screen. A catalog assertion alone cannot catch that, so this
 * mounts the real component under a non-English language and reads what the user
 * would read. Same shape as ReasoningEffortDropdown.i18n.test.tsx.
 */

import { describe, it, expect, vi, afterAll } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'

import Composer from '../apps/design-critique/Composer'
import { i18next } from '../i18n/index'

const noop = () => {}

const baseProps = {
  staged: [],
  dragging: false,
  blocked: null,
  showAuth: false,
  busy: false,
  err: '',
  inputRef: React.createRef<HTMLInputElement>(),
  onPick: vi.fn(),
  onDrop: vi.fn(),
  onDragOver: vi.fn(),
  onDragLeave: vi.fn(),
  pickFile: noop,
  dropStaged: noop,
  moveStaged: noop,
  clearStaged: noop,
  start: noop,
  setRefText: noop,
  setBlocked: noop,
  setShowAuth: noop,
  onTryAgain: noop,
} as unknown as React.ComponentProps<typeof Composer>

const FIGMA = 'https://www.figma.com/file/abc/Design'
const REPO = 'https://github.com/owner/repo'

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('Design Critique composer - target-kind localisation', () => {
  it('renders the English noun in English, unchanged', async () => {
    await i18next.changeLanguage('en')
    render(<Composer {...baseProps} refText={FIGMA} />)
    expect(screen.getByText('Figma file')).toBeTruthy()
  })

  it('renders the localised noun in Chinese, not raw English', async () => {
    await i18next.changeLanguage('zh-CN')
    const expected = i18next.t('apps.designCritique.constants.kind_figma') as string
    // Guard the guard: if the catalog lacked the key, i18next would fall back to
    // English and this test would pass while the bug persisted.
    expect(expected).not.toBe('Figma file')

    render(<Composer {...baseProps} refText={FIGMA} />)
    expect(screen.getByText(expected)).toBeTruthy()
    expect(screen.queryByText('Figma file')).toBeNull()
  })

  it('localises every kind it recognises, in every authored language', async () => {
    const cases = [
      { refText: FIGMA, key: 'apps.designCritique.constants.kind_figma' },
      { refText: REPO, key: 'apps.designCritique.constants.kind_repo' },
      { refText: '/Users/me/app', key: 'apps.designCritique.constants.kind_local' },
      { refText: 'http://localhost:3000', key: 'apps.designCritique.constants.kind_url' },
    ]
    for (const lang of ['bn', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh-CN']) {
      await i18next.changeLanguage(lang)
      for (const c of cases) {
        const expected = i18next.t(c.key) as string
        const { unmount } = render(<Composer {...baseProps} refText={c.refText} />)
        expect(screen.getByText(expected)).toBeTruthy()
        unmount()
      }
    }
  })

  it('says Unrecognised, not an empty bold token, for input it cannot place', async () => {
    await i18next.changeLanguage('en')
    render(<Composer {...baseProps} refText={'not a target'} />)
    expect(screen.getByText('Unrecognised')).toBeTruthy()
  })
})
