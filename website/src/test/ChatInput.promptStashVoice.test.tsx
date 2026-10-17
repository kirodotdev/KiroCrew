/**
 * The prompt stash waits while dictation is live.
 *
 * Streaming dictation rebuilds the composer text from a snapshot it took when
 * capture began. A stash under it would clear the text and paste blocks, and
 * the next partial would write the snapshot back with paste tokens whose
 * blocks are gone, so the send would carry literal tokens instead of their
 * content. A restore would be overwritten the same way. Both actions are
 * inert until recording and transcription settle.
 *
 * Dictation state reaches `ChatInput` from the Composer root's voice atom, not
 * from props, so the slice is what this drives.
 */
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import { addStashEntry, loadPromptStash, makeStashEntry } from '../utils/promptStash'
import { SlotProvider } from '../providers/SlotContext'

const voiceInput: { current: Record<string, unknown> } = { current: {} }

vi.mock('../chat-core/composer/Composer', async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    '../chat-core/composer/Composer',
  )
  return { ...actual, useComposerVoiceSlice: () => ({ inputProps: voiceInput.current }) }
})

const ChatInput = (await import('../components/ChatInput')).default
const { renderWithProviders } = await import('./helpers')

const paste: PasteBlock = { id: 'stash-paste', seq: 1, lines: 3, content: 'one\ntwo\nthree' }

function Host({ initial = '', initialBlocks = [] as PasteBlock[] }) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState<PasteBlock[]>(initialBlocks)
  return (
    <SlotProvider slotId="chat-1">
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
        promptStash
      />
      <output data-testid="blocks">{blocks.map(b => b.id).join(',')}</output>
    </SlotProvider>
  )
}

const pressStash = (el: Element) => fireEvent.keyDown(el, { key: 's', ctrlKey: true })

beforeEach(() => {
  localStorage.clear()
  voiceInput.current = {}
})
afterEach(() => {
  voiceInput.current = {}
})

describe('ChatInput prompt stash during dictation', () => {
  it.each([
    ['recording', { voiceRecording: true }],
    ['transcribing', { voiceTranscribing: true }],
  ])('leaves the draft and stash untouched while dictation is %s', (_label, voice) => {
    expect(addStashEntry('chat-1', makeStashEntry('kept', []))).toBe(true)
    voiceInput.current = voice
    const draft = `see ${formatToken(paste)}`
    const { unmount } = renderWithProviders(<Host initial={draft} initialBlocks={[paste]} />)
    const input = screen.getByRole('textbox')
    pressStash(input)
    expect(input).toHaveValue(draft)
    expect(screen.getByTestId('blocks')).toHaveTextContent('stash-paste')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['kept'])
    unmount()
    renderWithProviders(<Host />)
    const empty = screen.getByRole('textbox')
    pressStash(empty)
    expect(empty).toHaveValue('')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['kept'])
  })

  it('stashes again once dictation has settled', () => {
    renderWithProviders(<Host initial="after dictation" />)
    const input = screen.getByRole('textbox')
    pressStash(input)
    expect(input).toHaveValue('')
    expect(loadPromptStash('chat-1').map(e => e.text)).toEqual(['after dictation'])
  })
})
