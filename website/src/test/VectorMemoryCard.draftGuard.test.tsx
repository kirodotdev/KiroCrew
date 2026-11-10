// The Semantic card's drafts -- a key or value typed but not Set, an inline
// edit that differs from the stored value -- are unsaved work: the card reports
// them through `onDirtyChange`, which the Memory view's leave guard reads, so
// leaving the view asks before it drops them. (#32024)

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import VectorMemoryCard from '../pages/overview/VectorMemoryCard'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    vectorStats: vi.fn(async () => ({ semantic_active: 1, episodic_active: 0, embedded_count: 0, migrated: true })),
    vectorEmbeddingStatus: vi.fn(async () => ({ provider: 'none', setup_step: 'idle' })),
    vectorSemantic: vi.fn(async () => ({ entries: [{ key: 'pref.editor', value_json: '"vim"', confidence: 1.0, source: 'user_explicit' }] })),
    vectorSemanticWrite: vi.fn(async () => undefined),
    vectorEpisodic: vi.fn(async () => ({ entries: [] })),
    vectorEvents: vi.fn(async () => ({ events: [] })),
    vectorContextPreview: vi.fn(async () => null),
  },
}))

beforeEach(() => vi.clearAllMocks())

const editName = 'Edit value of “pref.editor”'

describe('VectorMemoryCard drafts', () => {
  it('reports a typed key and value as unsaved until Set writes them', async () => {
    const onDirtyChange = vi.fn()
    renderWithProviders(<VectorMemoryCard onDirtyChange={onDirtyChange} />)
    const key = await screen.findByRole('combobox', { name: 'Key' })
    expect(onDirtyChange).toHaveBeenLastCalledWith(false)

    fireEvent.change(key, { target: { value: 'pref.shell' } })
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)
    fireEvent.change(screen.getByRole('textbox', { name: 'Value' }), { target: { value: 'zsh' } })
    fireEvent.click(screen.getByRole('button', { name: 'Set' }))

    await waitFor(() => expect(api.vectorSemanticWrite).toHaveBeenCalledWith('pref.shell', 'zsh'))
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false))
  })

  it('reports an inline edit as unsaved only while it differs from the stored value', async () => {
    const onDirtyChange = vi.fn()
    renderWithProviders(<VectorMemoryCard onDirtyChange={onDirtyChange} />)
    fireEvent.click(await screen.findByRole('button', { name: editName }))
    const input = screen.getByRole('textbox', { name: editName })
    expect(onDirtyChange).toHaveBeenLastCalledWith(false)

    fireEvent.change(input, { target: { value: 'emacs' } })
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)
    fireEvent.change(input, { target: { value: 'vim' } })
    expect(onDirtyChange).toHaveBeenLastCalledWith(false)
    fireEvent.change(input, { target: { value: 'emacs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(onDirtyChange).toHaveBeenLastCalledWith(false)
    expect(api.vectorSemanticWrite).not.toHaveBeenCalled()
  })
})
