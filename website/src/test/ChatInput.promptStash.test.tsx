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
  withPromptStashLock,
} from '../utils/promptStash'
import { SlotProvider } from '../providers/SlotContext'
import { renderWithProviders } from './helpers'

const paste: PasteBlock = { id: 'stash-paste', seq: 1, lines: 3, content: 'one\ntwo\nthree' }

function Host({
  slotId = 'chat-1', initial = '', initialBlocks = [] as PasteBlock[], lexical = false,
  withPlusMenu = false, pendingFiles = [] as string[], promptStash = true,
  send,
}: {
  slotId?: string; initial?: string; initialBlocks?: PasteBlock[]; lexical?: boolean
  withPlusMenu?: boolean; pendingFiles?: string[]; promptStash?: boolean
  /** A host send: it clears the composer optimistically, as ChatPage and
   *  ChatPane do, and returns its delivery verdict. */
  send?: () => void | Promise<boolean>
}) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState<PasteBlock[]>(initialBlocks)
  return (
    <SlotProvider slotId={slotId || null}>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={send ? () => { setValue(''); return send() } : vi.fn()}
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
    const status = await screen.findByTestId('prompt-stash-status')
    expect(status).toHaveTextContent('Couldn’t stash: the stash is at its size limit. Your draft is still in the message input. To make room, send or clear it, then restore and send a stashed draft.')
    expect(status).not.toHaveTextContent('browser storage is full')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(loadPromptStash('chat-1')).toEqual([])
    expect(loadPromptStash('chat-other')).toHaveLength(2)
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent('the stash is at its size limit')

    // A draft that fits still stashes, and the refusal clears.
    fireEvent.change(input, { target: { value: 'small enough' } })
    pressStash(input)
    await waitFor(() => expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['small enough']))
    expect(input).toHaveValue('')
    expect(screen.queryByRole('alert')).toBeNull()
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
    seedStash('chat-1', [makeStashEntry('older', [], 1), makeStashEntry('newer', [], 2)])
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
    // Emptying the composer is not proof the draft was used (a voice
    // auto-submit clears it the same way), so it hands the pending copy back
    // to the stash instead of deleting it: both entries stay stored and the
    // count shows both again.
    act(() => { fireEvent.change(input, { target: { value: '' } }) })
    expect(await screen.findByTestId('prompt-stash-count')).toHaveTextContent('2 drafts stashed')
    await act(async () => { await withPromptStashLock(() => undefined) })
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['older', 'newer'])
    pressStash(input)
    expect(input).toHaveValue('newer')
    expect(screen.getByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
  })

  it('keeps the restored stash entry through edits until the draft leaves the composer', async () => {
    seedStash('chat-1', [makeStashEntry('safe copy', [])])
    renderWithProviders(<Host />)
    const input = screen.getByRole('textbox')

    pressStash(input)

    expect(input).toHaveValue('safe copy')
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['safe copy'])
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()

    // The composer's own draft save is debounced and can fail on quota or
    // never run if the tab dies, so an edit must not delete the only stored
    // copy. A lock request granted after the edit runs after any removal the
    // edit queued, so it is a barrier rather than a guess at a delay.
    fireEvent.change(input, { target: { value: 'safe copy edited' } })
    await act(async () => { await withPromptStashLock(() => undefined) })
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['safe copy'])
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()

    // Emptying the composer hands the copy back to the stash; it is never
    // deleted by an inference from the composer's contents.
    fireEvent.change(input, { target: { value: '' } })
    expect(await screen.findByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
    await act(async () => { await withPromptStashLock(() => undefined) })
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['safe copy'])
  })

  // A send's composer clear is optimistic. The entry goes only when the host
  // confirms delivery; a refusal, a rejection or a host with no verdict keeps
  // it in storage, where a reload finds it.
  it.each([
    ['refuses', () => Promise.resolve(false)],
    ['rejects', () => Promise.reject(new Error('503'))],
    ['returns no verdict', () => undefined],
  ] as const)('keeps a restored entry when the host send %s', async (_label, verdict) => {
    seedStash('chat-1', [makeStashEntry('sent once', [])])
    const send = vi.fn(verdict)
    renderWithProviders(<Host send={send} />)
    const input = screen.getByRole('textbox')
    pressStash(input)
    expect(input).toHaveValue('sent once')

    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    expect(send).toHaveBeenCalledTimes(1)
    expect(input).toHaveValue('')
    await act(async () => { await withPromptStashLock(() => undefined) })
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['sent once'])
    // Not delivered, so it is shown as stashed again.
    expect(await screen.findByTestId('prompt-stash-count')).toHaveTextContent('1 draft stashed')
  })

  it('removes a restored entry once the host confirms delivery, and hides it while in flight', async () => {
    seedStash('chat-1', [makeStashEntry('delivered', [])])
    let confirm: (ok: boolean) => void = () => {}
    const send = vi.fn(() => new Promise<boolean>((resolve) => { confirm = resolve }))
    renderWithProviders(<Host send={send} />)
    const input = screen.getByRole('textbox')
    pressStash(input)

    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' })
    await act(async () => { await withPromptStashLock(() => undefined) })
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['delivered'])
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()

    await act(async () => { confirm(true) })
    await act(async () => { await withPromptStashLock(() => undefined) })
    expect(loadPromptStash('chat-1')).toEqual([])
    expect(screen.queryByTestId('prompt-stash-count')).toBeNull()
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

  it('disables menu restore over a draft while the chord still stashes it', async () => {
    seedStash('chat-1', [makeStashEntry('older', [], 1), makeStashEntry('newer', [], 2)])
    renderWithProviders(<Host initial="typing now" withPlusMenu />)
    await openPlusMenu()
    expect(screen.getByTestId('prompt-stash-menu-stash')).toBeEnabled()
    const restore = screen.getByTestId('prompt-stash-menu-restore')
    expect(restore).toBeDisabled()
    expect(restore).toHaveTextContent('Restore stashed draft')
    expect(restore).toHaveTextContent('Stash or send your draft first.')
    fireEvent.click(restore)
    expect(screen.getByRole('textbox')).toHaveValue('typing now')
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['older', 'newer'])

    pressStash(screen.getByRole('textbox'))
    expect(screen.getByRole('textbox')).toHaveValue('')
    expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['older', 'newer', 'typing now'])
  })

  it('stashes from the + menu under an async lock when the host passes no paste blocks', async () => {
    installSerialPromptStashLocks()
    function BareHost() {
      const [value, setValue] = useState('menu draft')
      return (
        <SlotProvider slotId="chat-1">
          <ChatInput value={value} onChange={setValue} onSend={vi.fn()} onUploadFiles={vi.fn()} promptStash />
        </SlotProvider>
      )
    }
    renderWithProviders(<BareHost />)
    await openPlusMenu()
    fireEvent.click(screen.getByTestId('prompt-stash-menu-stash'))
    await waitFor(() => {
      expect(screen.getByRole('textbox')).toHaveValue('')
      expect(loadPromptStash('chat-1').map(entry => entry.text)).toEqual(['menu draft'])
    })
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
    expect(screen.getAllByTestId('prompt-stash-status').some(
      status => status.textContent?.includes('the stash is at its size limit'),
    )).toBe(true)
    expect(screen.queryByRole('alert')).toBeNull()
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

      expect(await screen.findByTestId('prompt-stash-status')).toHaveTextContent('the stash is at its size limit')
      expect(screen.queryByRole('alert')).toBeNull()
      expect(input).toHaveValue(draft)
      expect(loadPromptStash('chat-a')).toEqual([])
      expect(loadPromptStash('chat-concurrent').map(entry => entry.id)).toEqual([concurrent.id])
      expect(promptStashBytes()).toBeLessThanOrEqual(PROMPT_STASH_MAX_BYTES)
    } finally {
      setItem.mockRestore()
    }
  })

  it('offers a confirmed clear of other sessions\' drafts when the byte budget refuses a stash', async () => {
    // Entries a session deleted in another tab or by retention left behind:
    // nothing can restore them from here, yet they fill the shared budget.
    seedStash('chat-gone', [makeStashEntry('x'.repeat(500 * 1024), [])])
    const draft = 'a'.repeat(60 * 1024)
    renderWithProviders(<Host slotId="chat-a" initial={draft} />)
    const input = screen.getByRole('textbox')

    pressStash(input)

    expect(await screen.findByTestId('prompt-stash-status')).toHaveTextContent('the stash is at its size limit')
    expect(input).toHaveValue(draft)
    const offer = await screen.findByTestId('prompt-stash-clear-others')
    expect(offer).toHaveTextContent('Clear 1 draft stashed in other sessions')

    // The first press only asks again; nothing is deleted yet.
    fireEvent.click(offer)
    const confirm = await screen.findByTestId('prompt-stash-clear-others-confirm')
    expect(confirm).toHaveTextContent('Delete 1 stashed draft for good')
    expect(loadPromptStash('chat-gone')).toHaveLength(1)

    // A draft another tab stashes after the offer was counted is not part of it.
    seedStash('chat-later', [makeStashEntry('stashed after the offer', [])])
    fireEvent.click(confirm)

    await waitFor(() => expect(loadPromptStash('chat-gone')).toEqual([]))
    expect(loadPromptStash('chat-later').map(entry => entry.text)).toEqual(['stashed after the offer'])
    expect(screen.queryByTestId('prompt-stash-clear-others')).toBeNull()
    expect(await screen.findByTestId('prompt-stash-notice')).toHaveTextContent('Deleted 1 draft stashed in other sessions')

    pressStash(input)
    await waitFor(() => expect(input).toHaveValue(''))
    expect(loadPromptStash('chat-a').map(entry => entry.text)).toEqual([draft])
  })

  it('stacks the refusal over the clear offer and count so a narrow composer does not clip them', async () => {
    // GPT on 6fcd323abf: one local stashed draft plus a refusal caused by other
    // sessions puts the offer and the count on the row with the notice. Both
    // were shrink-0, so at 320px their widths overflowed ChatInput's
    // overflow-hidden wrapper. jsdom has no layout, so pin the classes that
    // make the row wrap and let the offer's label wrap.
    seedStash('chat-a', [makeStashEntry('kept here', [])])
    seedStash('chat-gone', [makeStashEntry('x'.repeat(500 * 1024), [])])
    renderWithProviders(<Host slotId="chat-a" initial={'a'.repeat(60 * 1024)} />)

    pressStash(screen.getByRole('textbox'))

    const offer = await screen.findByTestId('prompt-stash-clear-others')
    const line = screen.getByTestId('prompt-stash-status-line')
    expect(screen.getByTestId('prompt-stash-count')).toBeInTheDocument()
    expect(line).toHaveClass('flex-wrap')
    expect(screen.getByTestId('prompt-stash-notice')).toHaveClass('basis-full')
    expect(offer).not.toHaveClass('shrink-0')
    expect(offer).toHaveClass('min-w-0', 'break-words')
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
    expect(stash).toHaveTextContent(`Stash full (${PROMPT_STASH_MAX}). Send or clear this message, then restore and send a stashed draft to make room.`)
    const restore = screen.getByTestId('prompt-stash-menu-restore')
    expect(restore).toBeDisabled()
    expect(restore).toHaveTextContent('Stash or send your draft first.')
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
    expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('Remove attachments to stash or restore.')
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
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent(`Stash full (${PROMPT_STASH_MAX}). Send or clear this message, then restore and send a stashed draft to make room.`)
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
    expect(screen.getByTestId('prompt-stash-notice')).toHaveTextContent(`Stash full (${PROMPT_STASH_MAX}). Send or clear this message, then restore and send a stashed draft to make room.`)
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
    // The hook drops its one-shot cancel listener in a 0 ms timer. A 0 ms timer
    // queued after it fires after it, so this waits exactly for that cleanup.
    await new Promise(resolve => setTimeout(resolve, 0))
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
      expect(screen.getByTestId('prompt-stash-status')).toHaveTextContent('Remove attachments to stash or restore.')
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
    expect(first).toHaveTextContent('Remove attachments to stash or restore.')
    expect(fireEvent.keyDown(input, { key: 's', ctrlKey: true, repeat: true })).toBe(false)
    expect(screen.getByTestId('prompt-stash-status-text')).toBe(first)
    expect(pressStash(input)).toBe(false)
    const second = screen.getByTestId('prompt-stash-status-text')
    expect(second).not.toBe(first)
    expect(second).toHaveTextContent('Remove attachments to stash or restore.')
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
    // components/chat-input/engine.tsx loads LexicalComposerInput through React.lazy.
    const input = await screen.findByRole('textbox', undefined, { timeout: 5_000 })
    await waitFor(() => expect(input).toHaveAttribute('data-lexical-composer'))
    act(() => { pressStash(input) })
    await waitFor(() => expect(input).toHaveTextContent(''))
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['lexical draft'])
    act(() => { pressStash(input) })
    await waitFor(() => expect(input).toHaveTextContent('lexical draft'))
  })

  // Lexical routes UNDO_COMMAND to the host history (`onHistoryStep`), so a
  // restore must reseed that history in this composer too, or Ctrl+Z empties
  // the composer and the pending stash entry is consumed with the draft.
  it('does not let Ctrl+Z undo a restore in the Lexical composer', async () => {
    renderWithProviders(<Host initial="lexical restore" lexical />)
    const input = await screen.findByRole('textbox', undefined, { timeout: 5_000 })
    await waitFor(() => expect(input).toHaveAttribute('data-lexical-composer'))
    act(() => { pressStash(input) })
    await waitFor(() => expect(input).toHaveTextContent(''))
    act(() => { pressStash(input) })
    await waitFor(() => expect(input).toHaveTextContent('lexical restore'))
    act(() => { fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true }) })
    await act(async () => {})
    expect(input).toHaveTextContent('lexical restore')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['lexical restore'])
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

})
