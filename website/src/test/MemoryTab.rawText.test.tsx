/**
 * The Lessons table shows a lesson's rule and an unlabelled source exactly as
 * stored. React escapes text children itself, so a pre-escaped string would
 * show its entities (`&gt;&gt;`, `&amp;`) literally.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const RULE = 'Append with >> and run & wait, never <overwrite>'
const SOURCE = 'cron&probe>x'

const { api } = vi.hoisted(() => ({
  api: {
    lessons: vi.fn(),
    memoryPreferences: vi.fn(),
    memoryProjects: vi.fn(),
    memoryHistory: vi.fn(),
    memorySettings: vi.fn(),
    sessions: vi.fn(),
    memoryStores: vi.fn(),
    memoryRetired: vi.fn(),
    memoryBackups: vi.fn(),
    memoryCarve: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api }))
vi.mock('../pages/overview/VectorMemoryCard', () => ({
  SEMANTIC_CARD_ID: 'memory-semantic-card',
  default: () => <div />,
}))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div /> }))
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div /> }))
const { default: MemoryTab } = await import('../pages/overview/MemoryTab')

beforeEach(() => {
  vi.clearAllMocks()
  api.lessons.mockResolvedValue({
    lessons: [{ rule: RULE, category: 'tool', ts: '2026-01-03T00:00:00Z', scope: 'global', source: SOURCE }],
  })
  api.memoryPreferences.mockResolvedValue({ content: '' })
  api.memoryProjects.mockResolvedValue({ content: '' })
  api.memoryHistory.mockResolvedValue({ content: '' })
  api.memorySettings.mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false })
  api.sessions.mockResolvedValue({ sessions: [] })
  api.memoryStores.mockResolvedValue({ stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }] })
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryBackups.mockResolvedValue({ backups: [] })
  api.memoryCarve.mockResolvedValue({ counts: {} })
})

describe('Lessons table — raw lesson text', () => {
  it('shows the rule and an unlabelled source with >> and & exactly as stored', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const ruleCell = await screen.findByText(RULE)
    expect(ruleCell.textContent).toBe(RULE)
    const row = ruleCell.closest('tr')!
    expect(within(row).getByText(SOURCE).textContent).toBe(SOURCE)
    expect(row.textContent).not.toMatch(/&(gt|lt|amp);/)
  })
})
