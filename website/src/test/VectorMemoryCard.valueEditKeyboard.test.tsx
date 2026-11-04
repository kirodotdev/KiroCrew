// A semantic memory value opens its inline editor from the keyboard, and every
// close (Enter, Escape, the Save and Cancel buttons) hands focus back to the
// value's own button instead of stranding it on document.body. A mouse click on
// the cell keeps opening the editor. (#26667)

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(async () => ({ semantic_active: 1, episodic_active: 0, embedded_count: 0, migrated: true })),
    vectorEmbeddingStatus: vi.fn(async () => ({ provider: 'none', setup_step: 'idle' })),
    vectorSemantic: vi.fn(async () => ({ entries: [{ key: 'pref.editor', value_json: '"vim"', confidence: 1.0, source: 'user_explicit' }] })),
    vectorSemanticWrite: vi.fn(async () => undefined),
    vectorSemanticDelete: vi.fn(async () => undefined),
    vectorEpisodic: vi.fn(async () => ({ entries: [] })),
    vectorEvents: vi.fn(async () => ({ events: [] })),
    vectorContextPreview: vi.fn(async () => null),
  },
}))

afterEach(cleanup)
beforeEach(() => vi.clearAllMocks())

const editName = 'Edit value of “pref.editor”'
const valueButton = () => screen.getByRole('button', { name: editName })
const editor = () => screen.getByRole('textbox', { name: editName })
const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

async function openFromKeyboard() {
  const user = userEvent.setup()
  renderWithProviders(<VectorMemoryCard />)
  const button = await screen.findByRole('button', { name: editName })
  expect(button).toHaveAccessibleDescription('vim')
  act(() => { button.focus() })
  await user.keyboard('{Enter}')
  await waitFor(() => expect(editor()).toHaveFocus())
  return { user, input: editor() }
}

describe('VectorMemoryCard semantic value editor, keyboard', () => {
  it('Enter on the value button opens the editor; Enter saves and returns focus to it', async () => {
    const { input } = await openFromKeyboard()
    fireEvent.change(input, { target: { value: 'emacs' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.vectorSemanticWrite).toHaveBeenCalledWith('pref.editor', 'emacs'))
    await waitFor(() => expect(valueButton()).toHaveFocus())
  })

  it('Escape cancels and returns focus to the value button', async () => {
    const { input } = await openFromKeyboard()
    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(valueButton()).toHaveFocus())
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
  })

  it('Space opens the editor too', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    const button = await screen.findByRole('button', { name: editName })
    act(() => { button.focus() })
    await user.keyboard(' ')
    await waitFor(() => expect(editor()).toHaveFocus())
  })

  it('the named Save button saves and returns focus to the value button', async () => {
    const { user } = await openFromKeyboard()
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.vectorSemanticWrite).toHaveBeenCalledWith('pref.editor', 'vim'))
    await waitFor(() => expect(valueButton()).toHaveFocus())
  })

  it('the named Cancel button closes and returns focus to the value button', async () => {
    const { user } = await openFromKeyboard()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(valueButton()).toHaveFocus())
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
  })

  it('a save that resolves after the user moved on leaves focus where they put it', async () => {
    let resolve!: () => void
    vi.mocked(api.vectorSemanticWrite).mockImplementationOnce(() => new Promise<undefined>(r => { resolve = () => r(undefined) }))
    const { input } = await openFromKeyboard()
    fireEvent.keyDown(input, { key: 'Enter' })
    const filter = screen.getByPlaceholderText('Filter by key or value…')
    act(() => { filter.focus() })
    await act(async () => { resolve() })
    await waitFor(() => expect(screen.queryByRole('textbox', { name: editName })).toBeNull())
    await nextFrame()
    expect(filter).toHaveFocus()
  })

  it('a mouse click on the cell still opens the editor', async () => {
    const user = userEvent.setup()
    renderWithProviders(<VectorMemoryCard />)
    const button = await screen.findByRole('button', { name: editName })
    await user.click(button.closest('td')!)
    await waitFor(() => expect(editor()).toHaveFocus())
  })
})
