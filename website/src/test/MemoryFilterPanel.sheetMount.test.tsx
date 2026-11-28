import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { i18next } from '../i18n/all'
import { renderWithProviders } from './helpers'
import { FilterPanel } from '../pages/overview/memoryPage/MemoryListParts'
import { DEFAULT_VIEW } from '../pages/overview/memoryPage/memoryListView'

const tags = Object.fromEntries(Array.from({ length: 60 }, (_, i) => [`tag${String(i).padStart(2, '0')}`, 100 - i]))

/** The phone sheet mounts its value rows over a few frames, and ends with every one. */
describe('FilterPanel sheet mounting', () => {
  it('opens with no rows and then mounts every value, each still a working checkbox', async () => {
    const onToggle = vi.fn()
    renderWithProviders(<FilterPanel view={DEFAULT_VIEW} facets={{ tag: tags }} onToggle={onToggle} onRange={() => {}} />)
    // The first frame holds no value rows; they mount from the next frame on.
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    // The rows mount on a per-frame time budget, so a busy test worker needs more frames.
    await waitFor(() => expect(screen.getAllByRole('checkbox')).toHaveLength(60), { timeout: 5000 })
    fireEvent.click(screen.getByRole('checkbox', { name: '#tag59' }))
    expect(onToggle).toHaveBeenCalledWith('tag', 'tag59')
  })

  it('mounts every row at once in the wide rail', () => {
    renderWithProviders(<FilterPanel capped view={DEFAULT_VIEW} facets={{ tag: tags }} onToggle={() => {}} onRange={() => {}} />)
    expect(screen.getAllByRole('checkbox')).toHaveLength(60)
  })

  it('matches the value search against the labels of the language now shown', async () => {
    renderWithProviders(<FilterPanel capped view={DEFAULT_VIEW} facets={{ scope: { '': 3 } }} onToggle={() => {}} onRange={() => {}} />)
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'all repos' } })
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
    try {
      // The Japanese label does not hold the English words, so the value no longer matches.
      await act(async () => { await i18next.changeLanguage('ja') })
      expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    } finally {
      await act(async () => { await i18next.changeLanguage('en') })
    }
  })
})
