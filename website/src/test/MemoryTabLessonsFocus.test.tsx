/**
 * `?focus=lessons` lands the global Memory view on its lessons: the Lessons card
 * when vector memory is off, the Semantic browser pre-filtered to `lesson.` rows
 * when it keeps them. The landing waits for the vector card's status so it never
 * scrolls to a Lessons card that is about to disappear.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { renderWithProviders } from './helpers'

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

/** What the stub vector card reports, and the props it was given. */
const vector = vi.hoisted(() => ({ active: false, settled: true, filter: undefined as string | undefined }))
function StubVectorCard({ onActiveChange, onSettledChange, initialSemanticFilter }: {
  onActiveChange?: (a: boolean) => void
  onSettledChange?: (s: boolean) => void
  initialSemanticFilter?: string
}) {
  vector.filter = initialSemanticFilter
  useEffect(() => { onActiveChange?.(vector.active) }, [onActiveChange])
  useEffect(() => { onSettledChange?.(vector.settled) }, [onSettledChange])
  return vector.active ? <div id="memory-semantic-card" data-testid="semantic-card" /> : <div />
}
vi.mock('../pages/overview/VectorMemoryCard', () => ({
  SEMANTIC_CARD_ID: 'memory-semantic-card',
  default: StubVectorCard,
}))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div /> }))
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div /> }))
const { default: MemoryTab, LESSONS_CARD_ID } = await import('../pages/overview/MemoryTab')

let scrolled: string[]
const original = Element.prototype.scrollIntoView

beforeEach(() => {
  vi.clearAllMocks()
  scrolled = []
  Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.id) }
  vector.active = false
  vector.settled = true
  vector.filter = undefined
  api.lessons.mockResolvedValue({ lessons: [] })
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
afterEach(() => {
  Element.prototype.scrollIntoView = original
})

describe('?focus=lessons', () => {
  it('lands on the Lessons card when vector memory is off', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} focus="lessons" />)
    await waitFor(() => expect(scrolled).toEqual([LESSONS_CARD_ID]))
    expect(vector.filter).toBe('lesson.')
  })

  it('lands on the Semantic browser when vector memory keeps the lessons', async () => {
    vector.active = true
    renderWithProviders(<MemoryTab refreshTrigger={0} focus="lessons" />)
    await waitFor(() => expect(scrolled).toEqual(['memory-semantic-card']))
    expect(document.getElementById(LESSONS_CARD_ID)).toBeNull()
  })

  it('does not land anywhere until the vector status has answered', async () => {
    vector.settled = false
    renderWithProviders(<MemoryTab refreshTrigger={0} focus="lessons" />)
    await waitFor(() => expect(document.getElementById(LESSONS_CARD_ID)).not.toBeNull())
    expect(scrolled).toEqual([])
  })

  it('leaves the page alone without the focus', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await waitFor(() => expect(document.getElementById(LESSONS_CARD_ID)).not.toBeNull())
    expect(scrolled).toEqual([])
    expect(vector.filter).toBeUndefined()
  })
})
