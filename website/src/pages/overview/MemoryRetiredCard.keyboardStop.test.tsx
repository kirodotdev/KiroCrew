/**
 * With no replaced experiences the table shows only its header and an empty
 * state, and nothing in it takes focus. While that table hides columns its
 * scroller is the named Tab stop the arrow keys scroll. A row brings its own
 * Restore button, which already scrolls the table when focused, so a table with
 * rows gets no extra stop.
 *
 * jsdom does no layout, so scroll geometry is stubbed the way
 * `MarkdownRenderer.tableKeyboardStop.test.tsx` stubs it, and the API is mocked
 * as `MemoryRetiredCard.truncate.test.tsx` mocks it.
 */
import { screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
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

/** `hidden` px of content beyond the right edge of a 320px scroller. */
function stubGeometry(hidden: number) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(0)
}

beforeEach(() => {
  vi.clearAllMocks()
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({})
  api.themeBoot.mockResolvedValue({})
})

afterEach(() => { vi.restoreAllMocks() })

describe('MemoryRetiredCard empty table keyboard stop', () => {
  it('makes a clipped table with no rows a Tab stop named after the card', async () => {
    stubGeometry(70)
    api.memoryRetired.mockResolvedValue({ retired: [] })
    renderWithProviders(<MemoryRetiredCard store="" />)
    const region = await screen.findByRole('region', { name: 'Replaced experiences' })
    expect(region).toHaveAttribute('tabindex', '0')
    expect(region.querySelector('table')).not.toBeNull()
  })

  it('leaves an empty table that fits out of the Tab order', async () => {
    stubGeometry(0)
    api.memoryRetired.mockResolvedValue({ retired: [] })
    renderWithProviders(<MemoryRetiredCard store="" />)
    const scroller = (await screen.findByRole('table')).parentElement!
    expect(scroller).not.toHaveAttribute('tabindex')
    expect(scroller).not.toHaveAttribute('role')
    expect(scroller).not.toHaveAttribute('aria-label')
  })

  it('adds no stop to a clipped table whose rows hold a Restore button', async () => {
    stubGeometry(70)
    api.memoryRetired.mockResolvedValue({
      retired: [{ id: 'r1', text: 'remembered text', superseded_by: null, retired_times: 1, ts: '2026-09-09T10:00:00Z' }],
    })
    renderWithProviders(<MemoryRetiredCard store="" />)
    const restore = await screen.findByRole('button', { name: 'Restore experience', exact: true })
    const scroller = restore.closest('table')!.parentElement!
    expect(scroller).not.toHaveAttribute('tabindex')
    expect(scroller).not.toHaveAttribute('role')
    expect(scroller).not.toHaveAttribute('aria-label')
  })
})
