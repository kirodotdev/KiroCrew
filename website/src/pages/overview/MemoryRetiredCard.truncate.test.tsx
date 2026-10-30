import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import MemoryRetiredCard from './MemoryRetiredCard'

const { api } = vi.hoisted(() => ({
  api: {
    memoryRetired: vi.fn(),
    memoryRestoreRetired: vi.fn(),
    // ThemeProvider in the shared wrapper reads these on mount.
    themes: vi.fn(),
    themeDetail: vi.fn(),
    themeBoot: vi.fn(),
  },
}))
vi.mock('../../api/client', () => ({ api }))

/** The preview is the first text node of the row's first cell; the `…` marker and
 *  the full text inside the details expander are later siblings. */
async function renderPreview(text: string): Promise<string> {
  api.memoryRetired.mockResolvedValue({
    retired: [{ id: 'r1', text, superseded_by: null, retired_times: 1, ts: '2026-09-09T10:00:00Z' }],
  })
  renderWithProviders(<MemoryRetiredCard store="" />)
  const restore = await screen.findByRole('button', { name: 'Restore experience', exact: true })
  return restore.closest('tr')!.querySelector('td')!.childNodes[0]?.textContent ?? ''
}

beforeEach(() => {
  vi.clearAllMocks()
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({})
  api.themeBoot.mockResolvedValue({})
})

describe('MemoryRetiredCard preview with astral characters', () => {
  it('drops an emoji the 240-unit cap would cut in half instead of keeping its high surrogate', async () => {
    // 239 ASCII units, then an emoji occupying UTF-16 units 239-240.
    expect(await renderPreview(`${'a'.repeat(239)}😀 the rest of the remembered sentence`)).toBe('a'.repeat(239))
  })

  it('keeps an emoji that ends exactly at the cap', async () => {
    expect(await renderPreview(`${'a'.repeat(238)}😀 the rest of the remembered sentence`)).toBe(
      `${'a'.repeat(238)}😀`,
    )
  })

  it('leaves long ASCII at 240 units', async () => {
    expect(await renderPreview('x'.repeat(300))).toBe('x'.repeat(240))
  })
})
