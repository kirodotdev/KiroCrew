import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import { addStashEntry, loadPromptStash, makeStashEntry, promptStashKey } from '../utils/promptStash'
import { SlotProvider } from '../providers/SlotContext'
import { renderWithProviders } from './helpers'

function Host({ lexical = false, withPlusMenu = false }: { lexical?: boolean; withPlusMenu?: boolean }) {
  const [value, setValue] = useState('')
  const [blocks, setBlocks] = useState<PasteBlock[]>([])
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
        lexicalComposer={lexical}
        onUploadFiles={withPlusMenu ? vi.fn() : undefined}
        promptStash
      />
      <output data-testid="blocks">{blocks.map(b => b.id).join(',')}</output>
    </SlotProvider>
  )
}

/** Press and release, as a keyboard does. Returns whether the keydown was left
 *  unprevented, as `fireEvent` reports it. */
const press = (el: Element, init: { key: string; code?: string; ctrlKey?: boolean }) => {
  const notPrevented = fireEvent.keyDown(el, init)
  fireEvent.keyUp(el, init)
  return notPrevented
}
const pressStash = (el: Element) => press(el, { key: 's', code: 'KeyS', ctrlKey: true })
const optionTexts = () => screen.getAllByRole('option').map(o => o.textContent)
const storedTexts = () => loadPromptStash('chat-1').map(e => e.text)
const stashKeys = () => Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i) ?? '')
  .filter(k => k.startsWith(promptStashKey('chat-1')))

const paste: PasteBlock = { id: 'picker-paste', seq: 1, lines: 3, content: 'one\ntwo\nthree' }

function seedThree() {
  const beta = `beta first line ${formatToken(paste)}\nbeta second`
  for (const entry of [
    makeStashEntry('alpha first line\nalpha second', [], 1),
    makeStashEntry(beta, [paste], 2),
    makeStashEntry('\ngamma first line\ngamma second', [], 3),
  ]) expect(addStashEntry('chat-1', entry)).toBe(true)
  return { beta }
}

async function openList(input: HTMLElement) {
  act(() => { pressStash(input) })
  return screen.findByRole('listbox')
}

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  queuedSendStash.clear()
})

describe('ChatInput prompt stash list', () => {
  it('opens a list of the drafts newest first on Ctrl+S in an empty input', async () => {
    seedThree()
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    const list = await openList(input)
    expect(list).toHaveAccessibleName('Stashed drafts')
    const options = screen.getAllByRole('option')
    // Each row is the draft's first non-blank line.
    expect(optionTexts()).toEqual(['gamma first line', expect.stringContaining('beta first line'), 'alpha first line'])
    expect(options[0]).toHaveAttribute('aria-selected', 'true')
    expect(options[1]).toHaveAttribute('aria-selected', 'false')
    expect(list).toHaveAttribute('aria-activedescendant', options[0].id)
    expect(list).toHaveFocus()
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('3 drafts stashed')
    expect(input).toHaveValue('')
  })

  it('restores the highlighted draft with ArrowDown then Enter and returns focus to the input', async () => {
    const { beta } = seedThree()
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    const list = await openList(input)
    act(() => { press(list, { key: 'ArrowDown' }) })
    expect(screen.getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true')
    act(() => { press(list, { key: 'Enter' }) })
    await waitFor(() => expect(input).toHaveValue(beta))
    expect(screen.getByTestId('blocks')).toHaveTextContent('picker-paste')
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('2 drafts stashed')
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Stashed draft restored. 2 drafts are still stashed.')
    expect(input).toHaveFocus()
  })

  it('closes on Escape without restoring and returns focus to the input', async () => {
    seedThree()
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    const list = await openList(input)
    const keys = stashKeys()
    act(() => { press(list, { key: 'Escape' }) })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(input).toHaveValue('')
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('3 drafts stashed')
    expect(keys).toHaveLength(3)
    expect(stashKeys()).toEqual(keys)
    expect(input).toHaveFocus()
  })

  it.each(['Delete', 'Backspace'])('discards the highlighted draft with %s and offers Undo', async (key) => {
    seedThree()
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    const list = await openList(input)
    const gammaId = loadPromptStash('chat-1')[2].id
    act(() => { press(list, { key }) })
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2))
    expect(stashKeys().some(k => k.endsWith(gammaId))).toBe(false)
    expect(stashKeys()).toHaveLength(2)
    expect(optionTexts()[0]).toContain('beta first line')
    // The highlight stays on the same position, now the next draft.
    expect(screen.getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Stashed draft discarded. 2 drafts are still stashed.')
    expect(input).toHaveValue('')
    // Reaching Undo takes focus off the list, which closes it.
    const undo = screen.getByTestId('prompt-stash-undo-discard')
    act(() => { undo.focus() })
    expect(screen.queryByRole('listbox')).toBeNull()
    fireEvent.click(undo)
    await waitFor(() => expect(storedTexts()).toHaveLength(3))
    expect(loadPromptStash('chat-1')[2].id).toBe(gammaId)
  })

  it('hands focus back to the input when the last entry in the list is discarded', async () => {
    expect(addStashEntry('chat-1', makeStashEntry('older', [], 1))).toBe(true)
    expect(addStashEntry('chat-1', makeStashEntry('newer', [], 2))).toBe(true)
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    const list = await openList(input)
    act(() => { press(list, { key: 'Delete' }) })
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(1))
    expect(list).toHaveFocus()
    act(() => { press(list, { key: 'Delete' }) })
    await waitFor(() => expect(storedTexts()).toEqual([]))
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(input).toHaveFocus()
    expect(input).toHaveValue('')
  })

  it('deletes only the clicked entry from its control, and restores an entry on click', async () => {
    seedThree()
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    const list = await openList(input)
    const beta = screen.getAllByRole('option')[1]
    const del = within(beta).getByTestId('prompt-stash-picker-delete')
    expect(del).toHaveAttribute('title', 'Discard stashed draft')
    // The control keeps focus on the list: its mousedown default is cancelled.
    expect(fireEvent.mouseDown(del)).toBe(false)
    fireEvent.mouseUp(del)
    fireEvent.click(del)
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(2))
    expect(optionTexts()).toEqual(['gamma first line', 'alpha first line'])
    expect(storedTexts()).toEqual(['alpha first line\nalpha second', '\ngamma first line\ngamma second'])
    expect(input).toHaveValue('')
    expect(list).toHaveFocus()
    fireEvent.click(screen.getAllByRole('option')[1])
    await waitFor(() => expect(input).toHaveValue('alpha first line\nalpha second'))
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
  })

  it('restores the highlighted draft on Ctrl+S inside the list and claims the chord', async () => {
    seedThree()
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    const list = await openList(input)
    let notPrevented = true
    act(() => { notPrevented = pressStash(list) })
    expect(notPrevented).toBe(false)
    await waitFor(() => expect(input).toHaveValue('\ngamma first line\ngamma second'))
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('restores the only draft at once, with no list', async () => {
    expect(addStashEntry('chat-1', makeStashEntry('only one', [], 1))).toBe(true)
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    act(() => { pressStash(input) })
    await waitFor(() => expect(input).toHaveValue('only one'))
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('closes the list, restoring nothing, when focus moves to the input', async () => {
    seedThree()
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    await openList(input)
    act(() => { input.focus() })
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(input).toHaveValue('')
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('3 drafts stashed')
    expect(storedTexts()).toHaveLength(3)
  })

  it('restores into the Lexical composer from the list and returns focus to it', async () => {
    seedThree()
    renderWithProviders(<Host lexical />)
    // components/chat-input/engine.tsx loads LexicalComposerInput through React.lazy.
    const input = await screen.findByRole('textbox', undefined, { timeout: 5_000 })
    await waitFor(() => expect(input).toHaveAttribute('data-lexical-composer'))
    const list = await openList(input)
    expect(list).toHaveFocus()
    act(() => { press(list, { key: 'Enter' }) })
    await waitFor(() => expect(input).toHaveTextContent('gamma first line'))
    expect(screen.queryByRole('listbox')).toBeNull()
    await waitFor(() => expect(input).toHaveFocus())
  })

  it('keeps the "+" menu Restore row restoring the latest at once', async () => {
    expect(addStashEntry('chat-1', makeStashEntry('older', [], 1))).toBe(true)
    expect(addStashEntry('chat-1', makeStashEntry('newer', [], 2))).toBe(true)
    renderWithProviders(<Host withPlusMenu />)
    const input = screen.getByRole('textbox')
    fireEvent.click(screen.getByTitle('Add files & options'))
    await screen.findByTestId('prompt-stash-menu')
    fireEvent.click(screen.getByTestId('prompt-stash-menu-restore'))
    await waitFor(() => expect(input).toHaveValue('newer'))
    expect(screen.queryByRole('listbox')).toBeNull()
  })
})
