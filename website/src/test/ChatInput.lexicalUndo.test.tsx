import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import { $getRoot, CONTROLLED_TEXT_INSERTION_COMMAND, type LexicalEditor } from 'lexical'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * Undo in the Lexical composer after a programmatic value change.
 *
 * The composer is controlled: every value the parent pushes in (an optimize
 * result, ↑/↓ recall) replaces the editor content and clears Lexical's own
 * history. The textarea path keeps an explicit snapshot history for exactly
 * that reason, and the Lexical path must use the same one. Otherwise, with the
 * Style Markdown While Typing setting on, Ctrl/Cmd+Z after Optimize could not
 * bring the original draft back.
 */

const ORIGINAL = 'my prompt'
const OPTIMIZED = 'a much better prompt'

function Host() {
  const [value, setValue] = useState(ORIGINAL)
  return (
    <>
      <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected={true} lexicalComposer />
      <output data-testid="value">{value}</output>
    </>
  )
}

const shown = () => screen.getByTestId('value').textContent

describe('ChatInput Lexical composer: undo uses the snapshot history', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return Promise.resolve({ ok: true, json: async () => ({ changed: true, optimized: OPTIMIZED }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('Ctrl+Z after Optimize restores the original draft, and Ctrl+Shift+Z redoes it', async () => {
    renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox')
    expect(input).toHaveAttribute('data-lexical-composer')
    fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
    await waitFor(() => expect(shown()).toBe(OPTIMIZED))

    fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true })
    await waitFor(() => expect(shown()).toBe(ORIGINAL))

    fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true, shiftKey: true })
    await waitFor(() => expect(shown()).toBe(OPTIMIZED))
  })

  it('Ctrl+Z after typing into the Lexical composer undoes the typed text through the same history', async () => {
    renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox')
    const editor = (input as unknown as { __lexicalEditor: LexicalEditor }).__lexicalEditor
    act(() => { editor.update(() => $getRoot().selectEnd(), { discrete: true }) })
    // One bulk insert is its own undo boundary in the snapshot history.
    act(() => { editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, ' plus a long typed tail') })
    await waitFor(() => expect(shown()).toBe(`${ORIGINAL} plus a long typed tail`))

    fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true })
    await waitFor(() => expect(shown()).toBe(ORIGINAL))

    fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true, shiftKey: true })
    await waitFor(() => expect(shown()).toBe(`${ORIGINAL} plus a long typed tail`))
  })
})
