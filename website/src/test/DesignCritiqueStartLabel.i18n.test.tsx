/**
 * The start button, in both places it is assembled.
 *
 * Design Critique's primary action is built by a ternary chain rather than a
 * single label, and the same two English fragments were assembled independently
 * in TWO files: `Composer.tsx` (before anything is staged) and
 * `ScopingPicker.tsx` (after discovery, once the user picks screens). Both read
 * `'Critique this flow · ' + n + ' screens'` and `'Critique this screen'` as raw
 * literals, so the button a non-English user presses to spend real time and
 * money read in English on a surface whose surrounding copy is translated.
 *
 * No existing gate could see it. `lint:i18n` counted the literals but its
 * per-file ceilings were satisfied; `catalogParity` and `check-i18n-keys` check
 * keys that exist, and these had none; the render gate measures pseudolocale
 * width, not language. What catches it is mounting the real components and
 * reading the button, which is what this file does.
 *
 * Two kinds of assertion here, and the distinction is the point:
 *
 *   - The ENGLISH tests are a PIN. They pass against `origin/main`'s components
 *     as well as these, which is what makes them evidence that no English
 *     output changed rather than a restatement of the new code.
 *   - The SENTINEL tests inject a non-English value for one key and require the
 *     rendered button to carry it. Those FAIL without the fix. A sentinel is
 *     needed because the 11 authored catalogs ship the English source verbatim
 *     for these keys (a translator has not passed over them yet), so a plain
 *     language switch renders identical text and could not tell a wired label
 *     from a hardcoded one.
 */

import React from 'react'
import { render, screen, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach, afterAll } from 'vitest'

import Composer from '../apps/design-critique/Composer'
import ScopingPicker from '../apps/design-critique/ScopingPicker'
import { i18next } from '../i18n/index'
import type { Scope, StagedItem } from '../apps/design-critique/types'

const noop = () => {}

/** A staged screenshot. Only `id` is read by the label branches under test. */
const staged = (id: string): StagedItem =>
  ({ id, url: 'blob:' + id, file: new File([''], id + '.png', { type: 'image/png' }) })

const composerProps = {
  staged: [],
  refText: '',
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

const scope = (n: number): Scope => ({
  screens: Array.from({ length: n }, (_, i) => ({ id: 's' + i, label: 'Screen ' + (i + 1) })),
  flows: [],
})

const pickerProps = (n: number, picked: string[]) => ({
  scope: scope(n),
  picked,
  refBrief: '',
  dragId: null,
  togglePick: noop,
  dropPickAt: noop,
  movePick: noop,
  useFlow: noop,
  setDragId: noop,
  setRefBrief: noop,
  runScoped: noop,
  onStartOver: noop,
}) as unknown as React.ComponentProps<typeof ScopingPicker>

/**
 * Put `value` at `key` in a language, run the body, then restore. `addResource`
 * with a language that already has a catalog merges into it, so only this key
 * moves and every other assertion in the suite still sees the real catalog.
 */
async function withSentinel(lng: string, key: string, value: string, body: () => void) {
  const previous = i18next.getResource(lng, 'translation', key) as string | undefined
  i18next.addResource(lng, 'translation', key, value)
  await i18next.changeLanguage(lng)
  try {
    body()
  } finally {
    if (previous !== undefined) i18next.addResource(lng, 'translation', key, previous)
    await i18next.changeLanguage('en')
  }
}

afterEach(() => cleanup())
afterAll(async () => { await i18next.changeLanguage('en') })

describe('Design Critique start button — English is unchanged', () => {
  it('names the flow and the screen count on the composer', async () => {
    await i18next.changeLanguage('en')
    render(<Composer {...composerProps} staged={[staged('a'), staged('b')]} />)
    expect(screen.getByRole('button', { name: /Critique this flow · 2 screens/ })).toBeTruthy()
  })

  it('names a single screen on the composer', async () => {
    await i18next.changeLanguage('en')
    render(<Composer {...composerProps} staged={[staged('a')]} />)
    expect(screen.getByRole('button', { name: /Critique this screen/ })).toBeTruthy()
  })

  it('names the flow and the picked count on the scoping step', async () => {
    await i18next.changeLanguage('en')
    render(<ScopingPicker {...pickerProps(3, ['s0', 's1'])} />)
    expect(screen.getByRole('button', { name: /Critique this flow · 2 screens/ })).toBeTruthy()
  })

  it('names a single screen on the scoping step', async () => {
    await i18next.changeLanguage('en')
    render(<ScopingPicker {...pickerProps(3, ['s0'])} />)
    expect(screen.getByRole('button', { name: /Critique this screen/ })).toBeTruthy()
  })

  it('asks for a pick before anything is picked, on the scoping step', async () => {
    await i18next.changeLanguage('en')
    render(<ScopingPicker {...pickerProps(3, [])} />)
    expect(screen.getByRole('button', { name: /Pick at least one screen/ })).toBeTruthy()
  })
})

describe('Design Critique start button — reads the catalog, not a literal', () => {
  it('takes the composer flow label from the catalog, count included', async () => {
    await withSentinel(
      'zh-CN',
      'apps.designCritique.composer.critique_this_flow_count_screens',
      'SENTINEL-COMPOSER-FLOW {{count}}',
      () => {
        render(<Composer {...composerProps} staged={[staged('a'), staged('b')]} />)
        expect(screen.getByRole('button', { name: /SENTINEL-COMPOSER-FLOW 2/ })).toBeTruthy()
        expect(screen.queryByText(/Critique this flow/)).toBeNull()
      },
    )
  })

  it('takes the composer single-screen label from the catalog', async () => {
    await withSentinel(
      'zh-CN',
      'apps.designCritique.composer.critique_this_screen',
      'SENTINEL-COMPOSER-ONE',
      () => {
        render(<Composer {...composerProps} staged={[staged('a')]} />)
        expect(screen.getByRole('button', { name: /SENTINEL-COMPOSER-ONE/ })).toBeTruthy()
      },
    )
  })

  it('takes the scoping flow label from the catalog, count included', async () => {
    await withSentinel(
      'zh-CN',
      'apps.designCritique.scopingPicker.critique_this_flow_count_screens',
      'SENTINEL-SCOPE-FLOW {{count}}',
      () => {
        render(<ScopingPicker {...pickerProps(3, ['s0', 's1'])} />)
        expect(screen.getByRole('button', { name: /SENTINEL-SCOPE-FLOW 2/ })).toBeTruthy()
        expect(screen.queryByText(/Critique this flow/)).toBeNull()
      },
    )
  })

  it('takes the scoping empty-pick label from the catalog', async () => {
    await withSentinel(
      'zh-CN',
      'apps.designCritique.scopingPicker.pick_at_least_one_screen',
      'SENTINEL-SCOPE-EMPTY',
      () => {
        render(<ScopingPicker {...pickerProps(3, [])} />)
        expect(screen.getByRole('button', { name: /SENTINEL-SCOPE-EMPTY/ })).toBeTruthy()
      },
    )
  })
})

describe('Design Critique start button — the two surfaces agree', () => {
  it('gives the composer and the scoping step the same English wording', () => {
    // Per-file namespaces are this repo's convention, so the same sentence is
    // stored twice. That is only safe while the two copies stay in step, and a
    // translator editing one and not the other is exactly how they drift.
    for (const leaf of ['critique_this_flow_count_screens', 'critique_this_screen']) {
      const a = i18next.getResource('en', 'translation', 'apps.designCritique.composer.' + leaf)
      const b = i18next.getResource('en', 'translation', 'apps.designCritique.scopingPicker.' + leaf)
      expect(a).toBeTruthy()
      expect(b).toEqual(a)
    }
  })
})
