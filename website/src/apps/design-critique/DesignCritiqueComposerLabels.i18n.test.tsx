/**
 * Design Critique composer and reading rail: every visible string comes from the
 * catalog, and the link field has an accessible name.
 *
 * Defects pinned here: the start button glued 'Critique ' to an English kind
 * noun, the staged hint glued the screen count to English text, the card title,
 * both link placeholders and the render hint were English literals, and the link
 * field had no name. The rail's reading lines on the page were English the same
 * way, including 'A ' + kind + ' — rendering it…'.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRef } from 'react'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { i18next } from '../../i18n/index'
import '../../i18n/all'
import Composer from './Composer'
import type { Blocked, StagedItem } from './types'

const SRC = path.resolve(__dirname, '../..')

function staged(n: number): StagedItem[] {
  return Array.from({ length: n }, (_, i) => ({
    id: `s-${i}`,
    file: new File(['x'], `s-${i}.png`, { type: 'image/png' }),
    url: `blob:s-${i}`,
  }))
}

function mount(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  const props = {
    staged: [] as StagedItem[],
    refText: '',
    dragging: false,
    blocked: null as Blocked | null,
    showAuth: false,
    busy: false,
    err: '',
    inputRef: createRef<HTMLInputElement>(),
    onPick: vi.fn(), onDrop: vi.fn(), onDragOver: vi.fn(), onDragLeave: vi.fn(), pickFile: vi.fn(),
    dropStaged: vi.fn(), moveStaged: vi.fn(), clearStaged: vi.fn(), start: vi.fn(), setRefText: vi.fn(),
    setBlocked: vi.fn(), setShowAuth: vi.fn(), onTryAgain: vi.fn(),
    ...overrides,
  }
  return render(<Composer {...props} />)
}

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('design critique composer labels', () => {
  it('keeps the English text and names the link field', () => {
    mount({ refText: 'https://github.com/o/r' })
    expect(screen.getByRole('heading', { name: 'What should I critique?' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Link to critique' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Critique repo/ })).toBeInTheDocument()
  })

  it('renders the composer in German', async () => {
    await i18next.changeLanguage('de')
    mount({ refText: 'https://github.com/o/r' })
    expect(screen.getByRole('heading', { name: 'Was soll ich kritisieren?' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Link zum Kritisieren' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Repo kritisieren/ })).toBeInTheDocument()
    expect(screen.getByText(/Ich klone es und liste die Bildschirme auf/)).toBeInTheDocument()
  })

  it('puts the screen count inside the Russian staged hint', async () => {
    await i18next.changeLanguage('ru')
    mount({ staged: staged(5) })
    expect(screen.getByText(/^5 экранов · это порядок сценария/)).toBeInTheDocument()
  })

  it.each([
    ['apps/design-critique/Composer.tsx', "'Critique ' + "],
    ['apps/design-critique/Composer.tsx', "' screens · this order is the flow order"],
    ['apps/design-critique/Composer.tsx', "'What should I critique?'"],
    ['apps/design-critique/Composer.tsx', "'Using your screenshots"],
    ['apps/design-critique/DesignCritiquePage.tsx', "'A ' + (KIND_LABEL"],
    ['apps/design-critique/DesignCritiquePage.tsx', "'A flow of ' + screens.length"],
  ])('%s takes its text from the catalog (%s)', (file, literal) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(literal)
  })
})
