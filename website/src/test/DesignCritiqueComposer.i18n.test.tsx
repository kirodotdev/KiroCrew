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
// `/all` for the 11 authored catalogs this file reads: `../i18n` registers
// English only.
import { i18next } from '../i18n/all'

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

/**
 * The other half of the same line.
 *
 * Localising the noun alone left the TAIL ("I'll pull the frames.") as a raw
 * English literal in utils.ts, appended into the same paragraph — so a French
 * paste read a French noun followed by an English sentence. These tests pin the
 * two things that can go wrong now that the tail is a catalog key: the English
 * must not have moved, and a translated value must actually reach the screen.
 *
 * Note what the second test does NOT claim. The 11 authored catalogs carry the
 * English source verbatim today, because writing sense in those languages is not
 * this author's to do, so a same-value assertion would pass whether or not the
 * wiring worked. Hence the sentinel: it proves the render follows the CATALOG,
 * which is what makes a later native pass land without another code change.
 */
describe('Design Critique composer - recognition tail', () => {
  const TAIL_KEY = 'apps.designCritique.utils.i_ll_pull_the_frames'

  it('pins the English recognition tail so it cannot drift untranslated', async () => {
    await i18next.changeLanguage('en')
    render(<Composer {...baseProps} refText={FIGMA} />)
    const noun = screen.getByText('Figma file')
    expect(noun.parentElement?.textContent).toBe('Figma file \u00b7 export the frames as PNGs and drop them in \u2014 I critique the exported images.')
  })

  it('reads the tail from the catalog, so translating it reaches the screen', async () => {
    await i18next.changeLanguage('zh-CN')
    const original = i18next.getResource('zh-CN', 'translation', TAIL_KEY) as string
    const sentinel = '\u62c9\u53d6\u753b\u6846\u3002'
    i18next.addResource('zh-CN', 'translation', TAIL_KEY, sentinel)
    try {
      render(<Composer {...baseProps} refText={FIGMA} />)
      // The tail is a bare text node beside the bold noun, so the assertion has
      // to read the whole line — which is the point: the line is one sentence.
      const line = screen.getByText(i18next.t('apps.designCritique.constants.kind_figma') as string).parentElement
      expect(line?.textContent).toContain(sentinel)
      expect(line?.textContent).not.toContain('I\u2019ll pull the frames.')
    } finally {
      i18next.addResource('zh-CN', 'translation', TAIL_KEY, original)
    }
  })

  it('keeps every tail present in every authored catalog', () => {
    const keys = [
      'i_ll_pull_the_frames',
      'i_ll_clone_it_and_list_the_screens_only_pages_th',
      'i_ll_list_the_screens_only_pages_that_render_wit',
      'it_must_be_running_right_now_i_ll_capture_it_liv',
      'i_ll_capture_it_and_measure_real_contrast_and_si',
      'not_something_i_recognise_give_me_a_figma_link_a',
    ]
    for (const lang of ['en', 'bn', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh-CN']) {
      for (const leaf of keys) {
        const value = i18next.getResource(lang, 'translation', `apps.designCritique.utils.${leaf}`)
        expect(typeof value === 'string' && value.length > 0, `${lang} missing ${leaf}`).toBe(true)
      }
    }
  })
})
