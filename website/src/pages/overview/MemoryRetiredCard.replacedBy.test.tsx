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

const row = (id: string, supersededBy: string | null) => ({ id, text: `remembered ${id}`, superseded_by: supersededBy, retired_times: 1, ts: '2026-09-09T10:00:00Z' })

beforeEach(() => {
  vi.clearAllMocks()
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({})
  api.themeBoot.mockResolvedValue({})
})

describe('MemoryRetiredCard Replaced by column', () => {
  it('leaves the column out when no row records what replaced it', async () => {
    // The global (V1) store writes no superseding key for what it retires.
    api.memoryRetired.mockResolvedValue({ retired: [row('r1', null), row('r2', '')] })
    renderWithProviders(<MemoryRetiredCard store="" />)
    await screen.findAllByRole('button', { name: 'Restore experience', exact: true })
    expect(screen.queryByRole('columnheader', { name: 'Replaced by' })).toBeNull()
    expect(screen.getAllByRole('columnheader')).toHaveLength(4)
  })

  it('shows the column once one row has a key', async () => {
    api.memoryRetired.mockResolvedValue({ retired: [row('r1', 'pref.color'), row('r2', null)] })
    renderWithProviders(<MemoryRetiredCard store="member-a" />)
    expect(await screen.findByRole('columnheader', { name: 'Replaced by' })).toBeInTheDocument()
    expect(screen.getByText('pref.color')).toBeInTheDocument()
  })

  it('spans the empty state across the columns it draws', async () => {
    api.memoryRetired.mockResolvedValue({ retired: [] })
    renderWithProviders(<MemoryRetiredCard store="" />)
    const empty = await screen.findByText('No replaced experiences')
    expect(empty.closest('td')).toHaveAttribute('colspan', '4')
  })
})
