// A read-only composer keeps its draft on the keys that edit it programmatically
// (#35447). `readOnly` stops native typing only, so each of these key paths
// checks it itself; #35448 covered Ctrl/Cmd+Enter and the arrow-up recall.
// `disabled` is the read-only state here: the textarea renders
// `readOnly={optimizing || disabled || typingLocked}`.
import { describe, it, expect, vi } from 'vitest'
import { useState } from 'react'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const block: PasteBlock = { id: 'p1', seq: 1, lines: 40, content: 'TRACEBACK: boom\n...40 lines...' }
const token = formatToken(block)

// The real handler's answer for a Backspace just past `@a.ts`: the whole
// mention and its trailing space go (see ChatInput.chipRemoveUndo.test.tsx).
const onMentionKey = (text: string, selStart: number, _selEnd: number, key: string) => {
  const at = text.indexOf('@a.ts')
  if (key !== 'Backspace' || at < 0 || selStart !== at + '@a.ts'.length) return null
  return { value: text.slice(0, at) + text.slice(selStart + 1), caret: at }
}

function Host({ initial, disabled = false, blocks = [] }: { initial: string; disabled?: boolean; blocks?: PasteBlock[] }) {
  const [value, setValue] = useState(initial)
  const [pasteBlocks, setPasteBlocks] = useState(blocks)
  return (
    <ChatInput
      value={value}
      onChange={setValue}
      onSend={vi.fn()}
      disabled={disabled}
      pasteBlocks={pasteBlocks}
      onPasteBlocksChange={setPasteBlocks}
      onMentionKey={onMentionKey}
    />
  )
}

const input = () => screen.getByLabelText('Message input') as HTMLTextAreaElement

/** Caret at `at`, then the key; returns whether the browser keeps the key. */
function press(at: number, init: KeyboardEventInit) {
  const ta = input()
  ta.setSelectionRange(at, at)
  return fireEvent.keyDown(ta, init)
}

describe('ChatInput: a read-only composer keeps its draft (#35447)', () => {
  it('Tab on a list line leaves the line alone and keeps its focus move', () => {
    renderWithProviders(<Host initial="- item" disabled />)
    expect(input().readOnly).toBe(true)
    expect(press(4, { key: 'Tab' })).toBe(true)
    expect(input().value).toBe('- item')
  })

  it('Backspace just past a staged @mention keeps the mention', () => {
    renderWithProviders(<Host initial="see @a.ts here" disabled />)
    press('see @a.ts'.length, { key: 'Backspace' })
    expect(input().value).toBe('see @a.ts here')
  })

  it('Backspace just past a paste token keeps the token', () => {
    renderWithProviders(<Host initial={`x${token}`} blocks={[block]} disabled />)
    press(`x${token}`.length, { key: 'Backspace' })
    expect(input().value).toBe(`x${token}`)
  })

  it('Ctrl+Z after an edit takes no undo step, and is still consumed', () => {
    const { rerender } = renderWithProviders(<Host initial="" />)
    fireEvent.change(input(), { target: { value: 'hello' } })
    rerender(<Host initial="" disabled />)
    expect(input().readOnly).toBe(true)
    expect(press(5, { key: 'z', ctrlKey: true })).toBe(false)
    expect(input().value).toBe('hello')
  })
})
