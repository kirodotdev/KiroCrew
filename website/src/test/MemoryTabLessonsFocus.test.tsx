/**
 * `?focus=lessons` lands the global Memory view on its lessons: the Lessons card
 * when vector memory is off, the Semantic browser pre-filtered to `lesson.` rows
 * when it keeps them. The landing waits for the vector card's status so it never
 * scrolls to a Lessons card that is about to disappear.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
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
    vectorContextPreview: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api }))

/** What the vector memory status reads answer. */
const vector = vi.hoisted(() => ({ active: false, settled: true }))
vi.mock('../pages/overview/useVectorMemoryState', async importOriginal => ({
  ...await importOriginal<typeof import('../pages/overview/useVectorMemoryState')>(),
  useVectorMemoryState: () => ({ active: vector.active, migrated: false, settled: vector.settled }),
}))
const embed = vi.hoisted(() => ({ onDirtyChange: undefined as ((d: boolean) => void) | undefined }))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({
  default: ({ onDirtyChange }: { onDirtyChange?: (d: boolean) => void }) => { embed.onDirtyChange = onDirtyChange; return <div /> },
}))
/** The props the stub record list was last given. */
const list = vi.hoisted(() => ({ focusLessons: undefined as boolean | undefined, focusKey: undefined as string | undefined }))
vi.mock('../pages/overview/memoryPage/MemoryList', () => ({
  default: ({ focusLessons, focusKey }: { focusLessons?: boolean; focusKey?: string }) => { list.focusLessons = focusLessons; list.focusKey = focusKey; return <div data-testid="memory-list" /> },
}))
const { default: MemoryTab, LESSONS_CARD_ID } = await import('../pages/overview/MemoryTab')

let scrolled: string[]
const original = Element.prototype.scrollIntoView

beforeEach(() => {
  vi.clearAllMocks()
  scrolled = []
  Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this.id) }
  vector.active = false
  vector.settled = true
  list.focusLessons = undefined
  list.focusKey = undefined
  embed.onDirtyChange = undefined
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
  api.vectorContextPreview.mockResolvedValue({ session_start: { parts: [{ family: 'lessons', text: 'Work in a worktree' }] } })
})
afterEach(() => {
  Element.prototype.scrollIntoView = original
})

/** Whether the page holds a browser unload for unsaved work. */
function leave() {
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  return event.defaultPrevented
}

describe('?focus=lessons', () => {
  it('lands on the Lessons card when vector memory is off', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} focus="lessons" />)
    await waitFor(() => expect(scrolled).toEqual([LESSONS_CARD_ID]))
  })

  it('opens the Memories list on its Lessons pill when vector memory keeps the lessons', async () => {
    vector.active = true
    renderWithProviders(<MemoryTab refreshTrigger={0} focus="lessons" focusLessonKey="lesson.0123456789ab" />)
    await waitFor(() => expect(list.focusLessons).toBe(true))
    expect(list.focusKey).toBe('lesson.0123456789ab')
    expect(screen.getByTestId('memory-list')).toBeVisible()
    // The list lands on the lesson itself; nothing scrolls to a card.
    expect(scrolled).toEqual([])
    expect(document.getElementById(LESSONS_CARD_ID)).toBeNull()
  })

  it('does not land anywhere until the vector status has answered', async () => {
    vector.settled = false
    renderWithProviders(<MemoryTab refreshTrigger={0} focus="lessons" />)
    await waitFor(() => expect(document.getElementById(LESSONS_CARD_ID)).not.toBeNull())
    expect(scrolled).toEqual([])
  })

  it('counts a New memory draft as unsaved work until the dialog closes', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    fireEvent.click(await screen.findByRole('button', { name: 'New memory' }))
    const dialog = screen.getByRole('dialog', { name: 'New memory' })
    expect(leave()).toBe(false)
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Memory key' }), { target: { value: 'pref.editor' } })
    await waitFor(() => expect(leave()).toBe(true))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New memory' })).toBeNull())
    await waitFor(() => expect(leave()).toBe(false))
  })

  it('creates facts and lessons from the one New memory form, and the ⋯ menu offers no create action', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    fireEvent.click(await screen.findByRole('button', { name: 'New memory' }))
    const dialog = screen.getByRole('dialog', { name: 'New memory' })
    // The key field suggests only keys the store accepts.
    const hints = document.getElementById(within(dialog).getByRole('combobox', { name: 'Memory key' }).getAttribute('list')!)!
    const offered = Array.from(hints.querySelectorAll('option')).map(option => option.value)
    expect(offered.length).toBeGreaterThan(0)
    expect(offered.every(value => /^(pref|project|user)\./.test(value))).toBe(true)
    fireEvent.click(within(dialog).getByRole('radio', { name: 'Lesson' }))
    expect(within(dialog).getByRole('textbox', { name: 'Rule' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('combobox', { name: 'Memory key' })).toBeNull()
    expect(within(dialog).getByRole('button', { name: 'Save lesson' })).toBeDisabled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New memory' })).toBeNull())
    fireEvent.pointerDown(screen.getByRole('button', { name: 'More actions' }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    expect(await screen.findByRole('menuitem', { name: 'Test recall' })).toHaveAttribute('data-ui-location', 'memory.test-recall')
    expect(screen.queryByRole('menuitem', { name: /^New/ })).toBeNull()
  })

  it('opens Test recall on the global page while vector memory is off', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    // With vector memory off the record list is not mounted; the page still mounts the dialog.
    await waitFor(() => expect(document.getElementById(LESSONS_CARD_ID)).not.toBeNull())
    expect(screen.queryByTestId('memory-list')).toBeNull()
    fireEvent.pointerDown(screen.getByRole('button', { name: 'More actions' }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Test recall' }))
    const dialog = screen.getByRole('dialog', { name: 'Test recall' })
    expect(await within(within(dialog).getByTestId('recall-at-start')).findByText('Work in a worktree')).toBeVisible()
    expect(api.vectorContextPreview).toHaveBeenCalledWith(undefined, 'default')
  })

  it('counts a typed lesson that was not added yet as unsaved work', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const rule = await screen.findByRole('textbox', { name: 'Rule' })
    expect(leave()).toBe(false)
    fireEvent.change(rule, { target: { value: 'Always use tabs' } })
    await waitFor(() => expect(leave()).toBe(true))
  })

  it('counts a draft the embedding model card reports as unsaved work', async () => {
    const reporter = embed
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByRole('textbox', { name: 'Rule' })
    expect(leave()).toBe(false)
    act(() => reporter.onDirtyChange?.(true))
    expect(leave()).toBe(true)
    act(() => reporter.onDirtyChange?.(false))
    expect(leave()).toBe(false)
  })

  it('leaves the page alone without the focus', async () => {
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await waitFor(() => expect(document.getElementById(LESSONS_CARD_ID)).not.toBeNull())
    expect(scrolled).toEqual([])
  })
})
