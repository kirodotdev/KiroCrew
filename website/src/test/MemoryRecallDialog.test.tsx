import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

/**
 * Test recall shows the session-start build's own parts, each under the family
 * the server tagged it with: a start with no message, and the parts the same
 * start adds when the typed task is its first message.
 */
const { api } = vi.hoisted(() => ({ api: { vectorContextPreview: vi.fn() } }))
vi.mock('../api/client', () => ({ api }))
const { MemoryRecallDialog } = await import('../pages/overview/memoryPage/MemoryRecallDialog')

const STORE = 'member-reviewer-abc'
const MEMORY = '[Memory — stable user profile.]\nuser.timezone: America/Los_Angeles\n[End of memory]\n\n'
// An unclosed pointer before a provider's block must not hide the block.
const TOOLS = '[Memory tools]\nFor earlier facts, call memory_recall.\n'
const PROVIDER = '[Memory from Notes (read-only)]\n- Deploys go out on Tuesdays\n[End of memory from provider]\n'
const LESSONS = '[Learned corrections]\n- Work in a worktree\n[End of learned corrections]\n'
const ACTIVITY = '[Memory activity]\n- Moved the session store\n[End of memory activity]\n\n'
const parts = (query?: string) => [
  { family: 'memory', text: MEMORY },
  ...(query ? [{ family: 'memory', text: ACTIVITY }] : []),
  { family: 'memory', text: TOOLS },
  { family: 'lessons', text: LESSONS },
  { family: 'provider', text: PROVIDER },
]

beforeEach(() => {
  vi.clearAllMocks()
  api.vectorContextPreview.mockImplementation((query?: string) => Promise.resolve({ session_start: { parts: parts(query) } }))
})

const preText = (pane: HTMLElement) => within(pane).queryAllByText((_, node) => node?.tagName === 'PRE').map(node => node.textContent)

describe('Test recall', () => {
  it('shows each session-start part verbatim under its family, and what the typed task adds', async () => {
    renderWithProviders(<MemoryRecallDialog store={STORE} onClose={vi.fn()} />)
    const dialog = await screen.findByRole('dialog', { name: 'Test recall' })
    const start = within(dialog).getByTestId('recall-at-start')
    await waitFor(() => expect(preText(start)).toEqual([MEMORY, TOOLS, LESSONS, PROVIDER]))
    expect(within(start).getAllByText('Memory')).toHaveLength(2)
    expect(within(start).getByText('Lessons')).toBeVisible()
    expect(within(start).getByText('Memory provider (read-only)')).toBeVisible()
    expect(api.vectorContextPreview).toHaveBeenCalledWith(undefined, STORE)
    const message = within(dialog).getByTestId('recall-with-message')
    expect(within(message).getByText('Type a task to see what it recalls.')).toBeVisible()
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Task (optional)' }), { target: { value: 'fix the flaky CI test' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Test recall' }))
    await waitFor(() => expect(api.vectorContextPreview).toHaveBeenCalledWith('fix the flaky CI test', STORE))
    // Only the part the message added; the parts every start has stay in the first pane.
    await waitFor(() => expect(preText(message)).toEqual([ACTIVITY]))
  })
})
