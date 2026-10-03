import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import {
  PROMPT_STASH_MAX,
  PROMPT_STASH_MAX_BYTES,
  addStashEntry,
  loadPromptStash,
  makeStashEntry,
  promptStashBytes,
  promptStashKey,
} from '../utils/promptStash'
import { SlotProvider } from '../providers/SlotContext'
import { renderWithProviders } from './helpers'

const paste: PasteBlock = { id: 'stash-paste', seq: 1, lines: 3, content: 'one\ntwo\nthree' }

function Host({
  slotId = 'chat-1', initial = '', initialBlocks = [] as PasteBlock[], lexical = false,
  withPlusMenu = false, pendingFiles = [] as string[], promptStash = true,
}) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState<PasteBlock[]>(initialBlocks)
  return (
    <SlotProvider slotId={slotId || null}>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
        lexicalComposer={lexical}
        onUploadFiles={withPlusMenu ? vi.fn() : undefined}
        pendingFiles={pendingFiles}
        promptStash={promptStash}
      />
      <output data-testid="blocks">{blocks.map(b => b.id).join(',')}</output>
    </SlotProvider>
  )
}

const pressStash = (el: Element) => fireEvent.keyDown(el, { key: 's', ctrlKey: true })
const openPlusMenu = async (expectMenu = true) => {
  fireEvent.click(screen.getByTitle('Add files & options'))
  if (expectMenu) await screen.findByTestId('prompt-stash-menu')
}
const seedStash = (slot: string, entries: ReturnType<typeof makeStashEntry>[]) => {
  for (const entry of entries) expect(addStashEntry(slot, entry)).toBe(true)
}

const originalLocksDescriptor = Object.getOwnPropertyDescriptor(navigator, 'locks')
function installSerialPromptStashLocks() {
  let tail = Promise.resolve<unknown>(undefined)
  const request = vi.fn((_name: string, callback: () => unknown | PromiseLike<unknown>) => {
    const result = tail.then(callback)
    tail = result.then(() => undefined, () => undefined)
    return result
  })
  Object.defineProperty(navigator, 'locks', { configurable: true, value: { request } })
  return request
}

beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  queuedSendStash.clear()
  if (originalLocksDescriptor) Object.defineProperty(navigator, 'locks', originalLocksDescriptor)
  else Reflect.deleteProperty(navigator, 'locks')
})

describe('ChatInput prompt stash', () => {
  it('stashes the draft with Ctrl+S, clears the composer and shows the count', async () => {
    renderWithProviders(<Host initial="half-written prompt" />)
    const input = screen.getByRole('textbox')
    const notPrevented = pressStash(input)
    expect(notPrevented).toBe(false)
    expect(input).toHaveValue('')
    expect(await screen.findByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('Draft stashed.')
    // A visible confirmation that survives the clearing edit the stash made,
    // then goes on the user's next edit.
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Stashed. Ctrl+S in an empty message input brings it back.')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['half-written prompt'])
    fireEvent.change(input, { target: { value: 'next' } })
    expect(screen.queryByTestId('prompt-stash-notice')).toBeNull()
  })

  it('refuses a stash past the byte budget with its own message and keeps the draft', async () => {
    // Another session's two 200 KB drafts leave ~112 KB of the budget; the
    // 150 KB draft here does not fit, so it is refused and nothing is evicted.
    const fill = 'x'.repeat(200 * 1024)
    seedStash('chat-other', [makeStashEntry(fill, []), makeStashEntry(fill, [])])
    const draft = 'y'.repeat(150 * 1024)
    renderWithProviders(<Host initial={draft} />)
    const input = screen.getByRole('textbox')

    pressStash(input)

    expect(input).toHaveValue(draft)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Couldn’t stash: the stash is at its size limit. Restore or send a stashed draft first. Your draft is still in the message input.')
    expect(alert).not.toHaveTextContent('browser storage is full')
    expect(loadPromptStash('chat-1')).toEqual([])
    expect(loadPromptStash('chat-other')).toHaveLength(2)
    expect(screen.queryByTestId('prompt-stash-notice')).toBeNull()
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('')

    // A draft that fits still stashes, and the refusal clears.
    fireEvent.change(input, { target: { value: 'small enough' } })
    pressStash(input)
    await waitFor(() => expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['small enough']))
    expect(input).toHaveValue('')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('refuses a swap whose outgoing draft would break the byte budget and restores nothing', async () => {
    const fill = 'x'.repeat(200 * 1024)
    seedStash('chat-other', [makeStashEntry(fill, []), makeStashEntry(fill, [])])
    seedStash('chat-1', [makeStashEntry('kept', [])])
    const draft = 'y'.repeat(150 * 1024)
    renderWithProviders(<Host initial={draft} withPlusMenu />)
    await openPlusMenu()

    fireEvent.click(screen.getByTestId('prompt-stash-menu-restore'))

    expect(await screen.findByRole('alert')).toHaveTextContent('the stash is at its size limit')
    expect(screen.getByRole('textbox')).toHaveValue(draft)
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['kept'])
  })

  it('shows the count in the status line, outside the button cluster', async () => {
    seedStash('chat-1', [makeStashEntry('kept', [])])
    renderWithProviders(<Host />)
    const count = await screen.findByTestId('prompt-stash-count')
    expect(count.tagName).not.toBe('BUTTON')
    expect(count).toHaveTextContent('1 draft stashed')
    expect(count).toHaveAttribute('title', expect.stringContaining('Ctrl+S'))
    expect(count.className).not.toMatch(/cursor-pointer|hover:/)
    expect(count.parentElement).toHaveAttribute('data-testid', 'prompt-stash-status-line')
    expect(screen.queryByTestId('prompt-stash-badge')).toBeNull()
    expect(screen.queryByRole('button', { name: /stashed/ })).toBeNull()
  })

  it('does not paint a visible status line when the stash and notice are empty', () => {
    renderWithProviders(<Host />)
    expect(screen.queryByTestId('prompt-stash-status-line')).toBeNull()
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('')
    expect(screen.getByTestId('prompt-stash-status-text')).toBeEmptyDOMElement()
  })

  it('restores the latest entry with Ctrl+S on an empty composer', async () => {
    seedStash('chat-1', [makeStashEntry('older', []), makeStashEntry('newer', [])])
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    expect(await screen.findByTestId('prompt-stash-count')).toHaveTextContent('2 drafts stashed')
    pressStash(input)
    expect(input).toHaveValue('newer')
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['older', 'newer'])
    // A plain restore says so on screen, not only in the live region.
    const notice = await screen.findByTestId('prompt-stash-notice')
    expect(notice).toHaveTextContent('Stashed draft restored. 1 draft is still stashed.')
    expect(notice.className).toContain('text-text')
    // Clearing the composer (the send signal) consumes the pending copy, then
    // the next press restores the remaining entry without resurrecting it.
    act(() => { fireEvent.change(input, { target: { value: '' } }) })
    await waitFor(() => expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['older']))
    pressStash(input)
    expect(input).toHaveValue('older')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['older'])
    expect(await screen.findByTestId('prompt-stash-notice')).toHaveTextContent(/^Stashed draft restored\.$/)
    expect(screen.getByTestId('prompt-stash-status')).not.toHaveTextContent('0 drafts')
  })

  it('keeps the restored stash entry until a user edit consumes the draft', async () => {
    seedStash('chat-1', [makeStashEntry('safe copy', [])])
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')

    pressStash(input)

    expect(input).toHaveValue('safe copy')
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['safe copy'])
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()

    fireEvent.change(input, { target: { value: 'safe copy edited' } })
    await waitFor(() => expect(loadPromptStash('chat-1')).toEqual([]))
  })

  it('keeps the pending copy when re-stashing the restored draft fails', () => {
    seedStash('chat-1', [makeStashEntry('safe copy', [])])
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')
    pressStash(input)

    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
    try {
      pressStash(input)
      expect(input).toHaveValue('safe copy')
      expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['safe copy'])
    } finally {
      setItem.mockRestore()
    }
  })

  it('keeps the pending copy out of the count when a swap after a restore cannot write', async () => {
    seedStash('chat-1', [makeStashEntry('older', []), makeStashEntry('newer', [])])
    renderWithProviders(<Host withPlusMenu />)
    const input = screen.getByRole('textbox')
    pressStash(input)
    expect(input).toHaveValue('newer')
    // 'newer' is restored and still stored as the safe copy, so only 'older' counts.
    expect(await screen.findByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')

    await openPlusMenu()
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
    try {
      fireEvent.click(screen.getByTestId('prompt-stash-menu-restore'))
      expect(input).toHaveValue('newer')
      expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['older', 'newer'])
      expect(screen.getByTestId('prompt-stash-error')).toHaveTextContent('Couldn’t stash the draft')
      // The failed swap must not count the pending safe copy as stashed again.
      expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
    } finally {
      setItem.mockRestore()
    }
  })

  it('rotates the pending copy across repeated restores without duplicates', async () => {
    seedStash('chat-1', [makeStashEntry('older', []), makeStashEntry('newer', [])])
    renderWithProviders(<Host withPlusMenu />)
    const input = screen.getByRole('textbox')

    pressStash(input)
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['older', 'newer'])

    await openPlusMenu()
    fireEvent.click(screen.getByTestId('prompt-stash-menu-restore'))
    expect(input).toHaveValue('older')
    expect(loadPromptStash('chat-1').map(entry => entry.text).sort()).toEqual(['newer', 'older'])

    fireEvent.change(input, { target: { value: 'older edited' } })
    await waitFor(() => expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['newer']))
  })

  it('stashes and restores from the + menu, swapping with a draft in the composer', async () => {
    seedStash('chat-1', [makeStashEntry('older', []), makeStashEntry('newer', [])])
    renderWithProviders(<Host initial="typing now" withPlusMenu />)
    await openPlusMenu()
    expect(screen.getByTestId('prompt-stash-menu-stash')).toBeEnabled()
    const restore = screen.getByTestId('prompt-stash-menu-restore')
    expect(restore).toBeEnabled()
    expect(restore).toHaveTextContent('Swap with stashed draft')
    expect(restore).toHaveTextContent('your current text is stashed in its place')
    fireEvent.click(restore)
    expect(screen.getByRole('textbox')).toHaveValue('newer')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['older', 'newer', 'typing now'])
    // The swap says where the replaced text went, as a confirmation, not a warning.
    const swapNote = screen.getByTestId('prompt-stash-notice')
    expect(swapNote).toHaveTextContent('Restored. Your draft was stashed.')
    expect(swapNote.className).toContain('text-text')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('your draft was stashed')
    await openPlusMenu()
    fireEvent.click(screen.getByTestId('prompt-stash-menu-stash'))
    expect(screen.getByRole('textbox')).toHaveValue('')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['older', 'typing now', 'newer'])
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Stashed. Ctrl+S in an empty message input brings it back.')
    expect(screen.getByTestId('prompt-stash-notice').className).toContain('text-text')
  })

  it('adds the current draft before retaining the restored entry until use', async () => {
    seedStash('chat-1', [makeStashEntry('stashed', [])])
    const operations: string[] = []
    const originalSetItem = Storage.prototype.setItem
    const originalRemoveItem = Storage.prototype.removeItem
    const setSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, value) {
      operations.push('add')
      return Reflect.apply(originalSetItem, this, [key, value])
    })
    const removeSpy = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (key) {
      operations.push('remove')
      return Reflect.apply(originalRemoveItem, this, [key])
    })
    try {
      renderWithProviders(<Host initial="current" withPlusMenu />)
      await openPlusMenu()
      operations.length = 0
      fireEvent.click(screen.getByTestId('prompt-stash-menu-restore'))
      expect(operations).toEqual(['add'])
      const input = screen.getByRole('textbox')
      expect(input).toHaveValue('stashed')
      expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['stashed', 'current'])

      fireEvent.change(input, { target: { value: 'stashed edited' } })
      await waitFor(() => expect(operations).toEqual(['add', 'remove']))
      expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['current'])
    } finally {
      setSpy.mockRestore()
      removeSpy.mockRestore()
    }
  })

  it('uses one origin-wide lock to keep different-slot stashes within the byte budget', async () => {
    const request = installSerialPromptStashLocks()
    const fill = 'x'.repeat(400 * 1024)
    seedStash('chat-fill', [makeStashEntry(fill, [])])
    const draftA = 'a'.repeat(60 * 1024)
    const draftB = 'b'.repeat(60 * 1024)
    renderWithProviders(
      <>
        <Host slotId="chat-a" initial={draftA} />
        <Host slotId="chat-b" initial={draftB} />
      </>,
    )
    const inputs = screen.getAllByRole('textbox')

    pressStash(inputs[0])
    pressStash(inputs[1])

    await waitFor(() => {
      expect(inputs[0]).toHaveValue('')
      expect(inputs[1]).toHaveValue(draftB)
      expect(loadPromptStash('chat-a')).toHaveLength(1)
      expect(loadPromptStash('chat-b')).toEqual([])
    })
    expect(promptStashBytes()).toBeLessThanOrEqual(PROMPT_STASH_MAX_BYTES)
    expect(request).toHaveBeenCalledTimes(2)
    expect(request.mock.calls.every(([name]) => name === 'mc-prompt-stash')).toBe(true)
    expect(screen.getByRole('alert')).toHaveTextContent('the stash is at its size limit')
  })

  it('rolls back an unlocked write when a concurrent slot write crosses the byte budget', async () => {
    const fill = 'x'.repeat(400 * 1024)
    seedStash('chat-fill', [makeStashEntry(fill, [])])
    const draft = 'a'.repeat(60 * 1024)
    const concurrent = makeStashEntry('b'.repeat(60 * 1024), [])
    const concurrentKey = `${promptStashKey('chat-concurrent')}${concurrent.id}`
    const { id: _id, ...storedConcurrent } = concurrent
    renderWithProviders(<Host slotId="chat-a" initial={draft} />)
    const input = screen.getByRole('textbox')
    const originalSetItem = Storage.prototype.setItem
    let injected = false
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, value) {
      Reflect.apply(originalSetItem, this, [key, value])
      if (!injected && key.startsWith(promptStashKey('chat-a'))) {
        injected = true
        Reflect.apply(originalSetItem, this, [concurrentKey, JSON.stringify(storedConcurrent)])
      }
    })
    try {
      pressStash(input)

      expect(await screen.findByRole('alert')).toHaveTextContent('the stash is at its size limit')
      expect(input).toHaveValue(draft)
      expect(loadPromptStash('chat-a')).toEqual([])
      expect(loadPromptStash('chat-concurrent').map(entry => entry.id)).toEqual([concurrent.id])
      expect(promptStashBytes()).toBeLessThanOrEqual(PROMPT_STASH_MAX_BYTES)
    } finally {
      setItem.mockRestore()
    }
  })

  it('keeps a full shared stash capped when two composers swap', async () => {
    const request = installSerialPromptStashLocks()
    seedStash('chat-1', Array.from({ length: PROMPT_STASH_MAX }, (_, i) => makeStashEntry(`d${i}`, [], i)))
    renderWithProviders(
      <>
        <Host initial="first current draft" withPlusMenu />
        <Host initial="second current draft" withPlusMenu />
      </>,
    )
    const inputs = screen.getAllByRole('textbox')
    const plusMenus = screen.getAllByTitle('Add files & options')

    fireEvent.click(plusMenus[0])
    fireEvent.click(await screen.findByTestId('prompt-stash-menu-restore'))
    fireEvent.click(plusMenus[1])
    fireEvent.click(await screen.findByTestId('prompt-stash-menu-restore'))

    await waitFor(() => {
      expect(loadPromptStash('chat-1')).toHaveLength(PROMPT_STASH_MAX)
      expect(inputs[0]).not.toHaveValue('first current draft')
      expect(inputs[1]).not.toHaveValue('second current draft')
      expect(screen.getAllByTestId('prompt-stash-count')).toHaveLength(2)
      expect(screen.getAllByTestId('prompt-stash-count').every(node => node.textContent?.startsWith(`${PROMPT_STASH_MAX} drafts stashed`))).toBe(true)
    })
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('keeps pending consumption local to each composer during concurrent restores', async () => {
    const request = installSerialPromptStashLocks()
    seedStash('chat-1', [makeStashEntry('restore once', [])])
    renderWithProviders(
      <>
        <Host />
        <Host />
      </>,
    )
    const inputs = screen.getAllByRole('textbox')

    pressStash(inputs[0])
    pressStash(inputs[1])

    await waitFor(() => {
      expect(inputs.map(input => (input as HTMLTextAreaElement).value)).toEqual(['restore once', 'restore once'])
      expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['restore once'])
    })
    expect(request).toHaveBeenCalledTimes(2)
    expect(request.mock.calls.every(([name]) => name === 'mc-prompt-stash')).toBe(true)
  })

  it('disables the + menu items that would do nothing', async () => {
    renderWithProviders(<Host withPlusMenu />)
    await openPlusMenu()
    const stash = screen.getByTestId('prompt-stash-menu-stash')
    expect(stash).toBeDisabled()
    expect(stash).toHaveTextContent('Type something to stash it.')
    const restore = screen.getByTestId('prompt-stash-menu-restore')
    expect(restore).toBeDisabled()
    expect(restore).toHaveTextContent('Nothing is stashed yet.')
  })

  it('disables the stash item at the cap', async () => {
    seedStash('chat-1', Array.from({ length: PROMPT_STASH_MAX }, (_, i) => makeStashEntry(`d${i}`, [])))
    renderWithProviders(<Host initial="one too many" withPlusMenu />)
    await openPlusMenu()
    const stash = screen.getByTestId('prompt-stash-menu-stash')
    expect(stash).toBeDisabled()
    expect(stash).toHaveTextContent(`Stash full (${PROMPT_STASH_MAX}). Restore one first.`)
    expect(screen.getByTestId('prompt-stash-menu-restore')).toBeEnabled()
  })

  it('leaves the chord and every stash surface absent when opted out', async () => {
    seedStash('chat-1', [makeStashEntry('main composer draft', [])])
    renderWithProviders(<Host initial="off-record draft" withPlusMenu promptStash={false} />)
    const input = screen.getByRole('textbox')
    expect(pressStash(input)).toBe(true)
    expect(input).toHaveValue('off-record draft')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['main composer draft'])
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()
    expect(screen.queryByTestId('prompt-stash-status')).toBeNull()
    await openPlusMenu(false)
    expect(screen.queryByTestId('prompt-stash-menu')).toBeNull()
  })

  it('does not render the stash items without a session', async () => {
    renderWithProviders(<Host slotId="" withPlusMenu />)
    await openPlusMenu(false)
    expect(screen.getByText('Upload file')).toBeInTheDocument()
    expect(screen.queryByTestId('prompt-stash-menu')).toBeNull()
  })

  it('refuses the chord and the menu items while attachments are pending, touching nothing', async () => {
    seedStash('chat-1', [makeStashEntry('kept', [])])
    renderWithProviders(<Host initial="text with a file" withPlusMenu pendingFiles={['/tmp/a.png']} />)
    const input = screen.getByRole('textbox')
    const docSave = vi.fn()
    document.addEventListener('keydown', docSave)
    try {
      expect(pressStash(input)).toBe(false)
      expect(docSave).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', docSave)
    }
    expect(input).toHaveValue('text with a file')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['kept'])
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Remove attachments to stash or restore.')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('Attachments can’t be stashed.')
    await openPlusMenu()
    const stash = screen.getByTestId('prompt-stash-menu-stash')
    expect(stash).toBeDisabled()
    expect(stash).toHaveTextContent('Remove attachments to stash or restore.')
    const restore = screen.getByTestId('prompt-stash-menu-restore')
    expect(restore).toBeDisabled()
    // The disabled restore row names the same reason rather than the count.
    expect(restore).toHaveTextContent('Remove attachments to stash or restore.')
    expect(restore).not.toHaveAttribute('title')
  })

  it('refuses to restore over attachments even with an empty composer', () => {
    seedStash('chat-1', [makeStashEntry('kept', [])])
    renderWithProviders(<Host pendingFiles={['/tmp/a.png']} />)
    const input = screen.getByRole('textbox')
    expect(pressStash(input)).toBe(false)
    expect(input).toHaveValue('')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['kept'])
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Remove attachments')
  })

  it('follows another tab writing this session\'s stash', () => {
    renderWithProviders(<Host />)
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()
    seedStash('chat-1', [makeStashEntry('from another tab', [])])
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: promptStashKey('chat-1') }))
    })
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
    seedStash('chat-other', [makeStashEntry('unrelated', []), makeStashEntry('unrelated 2', [])])
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: promptStashKey('chat-other') }))
    })
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
  })

  it('keeps the stash across a remount, as after a reload', () => {
    const first = renderWithProviders(<Host initial="keep me" />)
    pressStash(screen.getByRole('textbox'))
    first.unmount()
    renderWithProviders(<Host />)
    pressStash(screen.getByRole('textbox'))
    expect(screen.getByRole('textbox')).toHaveValue('keep me')
  })

  it('stashes and restores the paste blocks behind the draft tokens', () => {
    renderWithProviders(<Host initial={`see ${formatToken(paste)}`} initialBlocks={[paste]} />)
    const input = screen.getByRole('textbox')
    pressStash(input)
    expect(screen.getByTestId('blocks')).toHaveTextContent('')
    pressStash(input)
    expect(input).toHaveValue(`see ${formatToken(paste)}`)
    expect(screen.getByTestId('blocks')).toHaveTextContent('stash-paste')
  })

  it('refuses at the cap and leaves the draft in the composer', () => {
    seedStash('chat-1', Array.from({ length: PROMPT_STASH_MAX }, (_, i) => makeStashEntry(`d${i}`, [])))
    renderWithProviders(<Host initial="one too many" />)
    const input = screen.getByRole('textbox')
    pressStash(input)
    expect(input).toHaveValue('one too many')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('The stash is full')
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent(`Stash full (${PROMPT_STASH_MAX}). Restore one first.`)
    expect(screen.getByTestId('prompt-stash-notice').className).toContain('text-warn')
    fireEvent.change(input, { target: { value: 'one too many!' } })
    expect(screen.queryByTestId('prompt-stash-notice')).toBeNull()
    expect(loadPromptStash('chat-1')).toHaveLength(PROMPT_STASH_MAX)
  })

  it('rolls back only its own unlocked write when a concurrent tab fills the stack', () => {
    Reflect.deleteProperty(navigator, 'locks')
    seedStash('chat-1', Array.from({ length: PROMPT_STASH_MAX - 1 }, (_, i) => makeStashEntry(`d${i}`, [])))
    renderWithProviders(<Host initial="racing draft" />)
    const input = screen.getByRole('textbox')
    const concurrent = makeStashEntry('concurrent winner', [])
    const originalSetItem = Storage.prototype.setItem
    let injected = false
    const setSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (key, value) {
      Reflect.apply(originalSetItem, this, [key, value])
      if (!injected && key.startsWith(promptStashKey('chat-1'))) {
        injected = true
        Reflect.apply(originalSetItem, this, [
          `${promptStashKey('chat-1')}${concurrent.id}`,
          JSON.stringify({ text: concurrent.text, blocks: concurrent.blocks, t: concurrent.t }),
        ])
      }
    })
    try {
      pressStash(input)
    } finally {
      setSpy.mockRestore()
    }

    expect(input).toHaveValue('racing draft')
    expect(loadPromptStash('chat-1')).toHaveLength(PROMPT_STASH_MAX)
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toContain('concurrent winner')
    expect(loadPromptStash('chat-1').map(entry => entry.text)).not.toContain('racing draft')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('The stash is full')
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent(`Stash full (${PROMPT_STASH_MAX}). Restore one first.`)
  })

  it('serializes concurrent stashes at nine entries so one succeeds and one is refused', async () => {
    const request = installSerialPromptStashLocks()
    seedStash('chat-1', Array.from({ length: PROMPT_STASH_MAX - 1 }, (_, i) => makeStashEntry(`d${i}`, [])))
    renderWithProviders(
      <>
        <Host initial="first racing draft" />
        <Host initial="second racing draft" />
      </>,
    )
    const inputs = screen.getAllByRole('textbox')

    pressStash(inputs[0])
    pressStash(inputs[1])

    await waitFor(() => {
      expect(loadPromptStash('chat-1')).toHaveLength(PROMPT_STASH_MAX)
      expect(inputs.map(input => (input as HTMLTextAreaElement).value).sort()).toEqual(['', 'second racing draft'])
    })
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toContain('first racing draft')
    expect(loadPromptStash('chat-1').map(entry => entry.text)).not.toContain('second racing draft')
    expect(screen.getAllByTestId('prompt-stash-status').some(status => status.textContent?.includes('The stash is full'))).toBe(true)
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('keeps the draft and shows an error when storage refuses the write', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError')
    })
    try {
      renderWithProviders(<Host initial="do not lose me" />)
      const input = screen.getByRole('textbox')
      pressStash(input)
      expect(input).toHaveValue('do not lose me')
      const error = screen.getByTestId('prompt-stash-error')
      expect(error).toHaveTextContent('Couldn’t stash the draft')
      // Spoken once: the alert announces itself, so the polite live region
      // must not carry a second copy of the same text.
      expect(error).toHaveAttribute('role', 'alert')
      const polite = screen.getByTestId('prompt-stash-status')
      expect(polite).toHaveAttribute('aria-live', 'polite')
      expect(polite).not.toHaveTextContent('Couldn’t stash the draft')
      expect(polite).toHaveTextContent('')
      expect(screen.getByTestId('prompt-stash-status-text')).toBeEmptyDOMElement()
      // The recovery half must stay visible on a phone: no one-line clamp on
      // this message, and so no tooltip standing in for the clipped text.
      expect(error).toHaveTextContent('Your draft is still in the message input.')
      const message = within(error).getByText(/Your draft is still in the message input/)
      expect(message.className).not.toContain('truncate')
      expect(message).not.toHaveAttribute('title')
      expect(within(error).queryByRole('button')).toBeNull()
      expect(error.className).not.toContain('max-sm:hidden')
      expect(error.parentElement).toHaveClass('text-left')
      expect(screen.queryByTestId('prompt-stash-count')).toBeNull()
      fireEvent.change(input, { target: { value: 'do not lose me!' } })
      expect(screen.queryByTestId('prompt-stash-error')).toBeNull()
    } finally {
      setItem.mockRestore()
    }
  })

  it('keeps each session its own stack', () => {
    seedStash('chat-other', [makeStashEntry('other session', [])])
    renderWithProviders(<Host />)
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()
    pressStash(screen.getByRole('textbox'))
    expect(screen.getByRole('textbox')).toHaveValue('')
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('Nothing is stashed')
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Nothing is stashed. Press Ctrl+S while the message input has text to stash it.')
  })

  it('does not reach document-level Cmd/Ctrl+S handlers when it claims the chord', () => {
    const docSave = vi.fn()
    document.addEventListener('keydown', docSave)
    try {
      renderWithProviders(<Host initial="draft" />)
      pressStash(screen.getByRole('textbox'))
      expect(docSave).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', docSave)
    }
  })

  it('passes a no-op press to document save handlers unprevented, then cancels the browser save dialog', () => {
    const docSave = vi.fn()
    document.addEventListener('keydown', docSave)
    try {
      const seenPrevented: boolean[] = []
      docSave.mockImplementation((ev: Event) => { seenPrevented.push(ev.defaultPrevented) })
      renderWithProviders(<Host />)
      // Document handlers see it unprevented; the browser default is still
      // cancelled afterwards, at window.
      expect(pressStash(screen.getByRole('textbox'))).toBe(false)
      expect(docSave).toHaveBeenCalledTimes(1)
      expect(seenPrevented).toEqual([false])
    } finally {
      document.removeEventListener('keydown', docSave)
    }
  })

  it('teaches the gesture in a neutral tone when an empty press finds nothing stashed', async () => {
    renderWithProviders(<Host />)
    pressStash(screen.getByRole('textbox'))
    const notice = await screen.findByTestId('prompt-stash-notice')
    expect(notice).toHaveTextContent('Nothing is stashed. Press Ctrl+S while the message input has text to stash it.')
    expect(notice.className).toContain('text-text')
    expect(notice.className).not.toContain('text-warn')
  })

  it('does not cancel a later, unrelated keydown after a no-op press', async () => {
    renderWithProviders(<Host />)
    pressStash(screen.getByRole('textbox'))
    await new Promise(resolve => setTimeout(resolve, 5))
    expect(fireEvent.keyDown(document.body, { key: 'a' })).toBe(true)
  })

  it('ignores key auto-repeat: a held chord stashes once and does not restore on the repeats', () => {
    renderWithProviders(<Host initial="held down" />)
    const input = screen.getByRole('textbox')
    expect(pressStash(input)).toBe(false)
    expect(input).toHaveValue('')
    // The repeats see an empty composer over a one-entry stack, which a fresh
    // press would restore. They are still claimed (no browser save dialog
    // while the key is down) but act on nothing.
    for (let i = 0; i < 3; i++) {
      expect(fireEvent.keyDown(input, { key: 's', ctrlKey: true, repeat: true })).toBe(false)
    }
    expect(input).toHaveValue('')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['held down'])
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('Stashed. Ctrl+S in an empty message input brings it back.')
    // Releasing and pressing again is a new press, and restores.
    expect(pressStash(input)).toBe(false)
    expect(input).toHaveValue('held down')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['held down'])
  })

  it('claims a repeat over pending attachments without re-announcing the refusal', () => {
    const docSave = vi.fn()
    document.addEventListener('keydown', docSave)
    try {
      renderWithProviders(<Host initial="draft" pendingFiles={['a.txt']} />)
      const input = screen.getByRole('textbox')
      expect(pressStash(input)).toBe(false)
      expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('Attachments can’t be stashed.')
      expect(fireEvent.keyDown(input, { key: 's', ctrlKey: true, repeat: true })).toBe(false)
      expect(docSave).not.toHaveBeenCalled()
      expect(input).toHaveValue('draft')
      expect(loadPromptStash('chat-1')).toEqual([])
    } finally {
      document.removeEventListener('keydown', docSave)
    }
  })

  it('announces a word-for-word repeated refusal again, but not on a held-key repeat', () => {
    // React bails out of a state update that sets the same string, so the live
    // region's text node would never change and the second refusal would be
    // silent. The node holding the text is keyed per announcement instead.
    seedStash('chat-1', Array.from({ length: PROMPT_STASH_MAX }, (_, i) => makeStashEntry(`d${i}`, [])))
    renderWithProviders(<Host initial="one too many" />)
    const input = screen.getByRole('textbox')
    const region = screen.getByTestId('prompt-stash-status')

    expect(pressStash(input)).toBe(false)
    const first = screen.getByTestId('prompt-stash-status-text')
    expect(first).toHaveTextContent('The stash is full')
    expect(region.childNodes).toHaveLength(1)

    // A held key repeats the press; the refusal is not re-announced.
    expect(fireEvent.keyDown(input, { key: 's', ctrlKey: true, repeat: true })).toBe(false)
    expect(screen.getByTestId('prompt-stash-status-text')).toBe(first)

    // Release and press again: same words, new text node, so it is spoken again.
    expect(pressStash(input)).toBe(false)
    const second = screen.getByTestId('prompt-stash-status-text')
    expect(second).not.toBe(first)
    expect(first.isConnected).toBe(false)
    expect(second).toHaveTextContent('The stash is full')
    // Replaced, not duplicated: the region still holds exactly one copy.
    expect(region.childNodes).toHaveLength(1)
    expect(region.textContent?.match(/The stash is full/g)).toHaveLength(1)
    expect(input).toHaveValue('one too many')
  })

  it('re-announces a repeated attachments refusal on a second press', () => {
    renderWithProviders(<Host initial="draft" pendingFiles={['a.txt']} />)
    const input = screen.getByRole('textbox')
    expect(pressStash(input)).toBe(false)
    const first = screen.getByTestId('prompt-stash-status-text')
    expect(first).toHaveTextContent('Attachments can’t be stashed.')
    expect(fireEvent.keyDown(input, { key: 's', ctrlKey: true, repeat: true })).toBe(false)
    expect(screen.getByTestId('prompt-stash-status-text')).toBe(first)
    expect(pressStash(input)).toBe(false)
    const second = screen.getByTestId('prompt-stash-status-text')
    expect(second).not.toBe(first)
    expect(second).toHaveTextContent('Attachments can’t be stashed.')
    expect(screen.getByTestId('prompt-stash-status').childNodes).toHaveLength(1)
  })

  it('leaves Ctrl+S alone outside the composer, and inside a composer with no session', () => {
    renderWithProviders(<Host initial="draft" slotId="" />)
    expect(pressStash(document.body)).toBe(true)
    const input = screen.getByRole('textbox')
    expect(pressStash(input)).toBe(true)
    expect(input).toHaveValue('draft')
  })

  it('does not touch the queued-send stash', () => {
    queuedSendStash.set('q1', { raw: 'queued', files: [], sent: 'queued' })
    renderWithProviders(<Host initial="draft" />)
    const input = screen.getByRole('textbox')
    pressStash(input)
    pressStash(input)
    expect(queuedSendStash.get('q1')?.raw).toBe('queued')
    expect(queuedSendStash.size).toBe(1)
  })

  it('works in the Lexical composer too', async () => {
    renderWithProviders(<Host initial="lexical draft" lexical />)
    const input = await screen.findByRole('textbox')
    await waitFor(() => expect(input).toHaveAttribute('data-lexical-composer'))
    act(() => { pressStash(input) })
    await waitFor(() => expect(input).toHaveTextContent(''))
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['lexical draft'])
    act(() => { pressStash(input) })
    await waitFor(() => expect(input).toHaveTextContent('lexical draft'))
  })

  it('does not let Ctrl+Z undo a restore while its safe copy is pending', () => {
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: 'restore me' } })
    pressStash(input) // stash: composer cleared, one entry
    expect(input).toHaveValue('')
    pressStash(input) // restore: entry remains pending in storage
    expect(input).toHaveValue('restore me')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['restore me'])
    // The restore reseeded the undo base, so there is nothing to step back to.
    fireEvent.keyDown(input, { key: 'z', ctrlKey: true })
    expect(input).toHaveValue('restore me')
    // Typing continues from the restored text rather than an emptied composer.
    fireEvent.change(input, { target: { value: 'restore me!' } })
    expect(input).toHaveValue('restore me!')
    // And a real edit made after the restore still undoes back to it.
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.keyDown(input, { key: 'z', ctrlKey: true })
    expect(input).toHaveValue('restore me!')
  })

  it('does not let Ctrl+Z undo a swap either', async () => {
    seedStash('chat-1', [makeStashEntry('older', [])])
    renderWithProviders(<Host initial="typing now" withPlusMenu />)
    const input = screen.getByRole('textbox') as HTMLTextAreaElement
    await openPlusMenu()
    fireEvent.click(screen.getByTestId('prompt-stash-menu-restore'))
    expect(input).toHaveValue('older')
    fireEvent.keyDown(input, { key: 'z', ctrlKey: true })
    expect(input).toHaveValue('older')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['older', 'typing now'])
  })

  it('reseeds same-text swaps with the restored paste blocks', async () => {
    const token = formatToken(paste)
    const restoredPaste: PasteBlock = {
      ...paste,
      id: 'restored-stash-paste',
      content: 'four\nfive\nsix',
    }
    seedStash('chat-1', [makeStashEntry(token, [restoredPaste])])
    renderWithProviders(<Host initial={token} initialBlocks={[paste]} withPlusMenu />)
    const input = screen.getByRole('textbox') as HTMLTextAreaElement

    await openPlusMenu()
    fireEvent.click(screen.getByTestId('prompt-stash-menu-restore'))
    expect(input).toHaveValue(token)
    expect(screen.getByTestId('blocks')).toHaveTextContent('restored-stash-paste')
    expect(loadPromptStash('chat-1')[0]?.blocks).toEqual([restoredPaste])

    fireEvent.change(input, { target: { value: `${token}!` } })
    await waitFor(() => expect(loadPromptStash('chat-1')[0]?.blocks).toEqual([paste]))
    fireEvent.keyDown(input, { key: 'z', ctrlKey: true })
    expect(input).toHaveValue(token)
    expect(screen.getByTestId('blocks')).toHaveTextContent('restored-stash-paste')

    fireEvent.change(input, { target: { value: `${token}?` } })
    expect(screen.getByTestId('blocks')).toHaveTextContent('restored-stash-paste')
    expect(loadPromptStash('chat-1')[0]?.blocks).toEqual([paste])
  })
})
