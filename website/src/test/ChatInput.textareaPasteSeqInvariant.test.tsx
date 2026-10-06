import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'

import ChatInput from '../components/ChatInput'
import { expandAll, formatToken, type PasteBlock } from '../utils/pasteTokens'
import { renderWithProviders } from './helpers'

const BLOCK: PasteBlock = {
  id: 'existing-paste',
  seq: 1,
  lines: 4,
  content: 'alpha\nbeta\ngamma\ndelta',
}
const TOKEN = formatToken(BLOCK)

function Host({ initial }: { initial: string }) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState([BLOCK])
  return (
    <>
      <ChatInput
        value={value}
        onChange={setValue}
        onSend={vi.fn()}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
        lexicalComposer={false}
      />
      <output data-testid="value">{value}</output>
      <output data-testid="blocks">{JSON.stringify(blocks)}</output>
    </>
  )
}

function pasteLiteralAt(
  selectionStart: number,
  selectionEnd: number = selectionStart,
  clipboard: string = TOKEN,
) {
  const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
  textarea.setSelectionRange(selectionStart, selectionEnd)
  const browserMayInsert = fireEvent.paste(textarea, {
    clipboardData: {
      types: ['text/plain'],
      items: [],
      getData: () => clipboard,
    },
  })
  if (browserMayInsert) {
    const next = textarea.value.slice(0, selectionStart) + clipboard + textarea.value.slice(selectionEnd)
    const caret = selectionStart + clipboard.length
    fireEvent.change(textarea, {
      target: { value: next, selectionStart: caret, selectionEnd: caret },
    })
  }
}

function hostPair(): { value: string; blocks: PasteBlock[] } {
  return {
    value: screen.getByTestId('value').textContent ?? '',
    blocks: JSON.parse(screen.getByTestId('blocks').textContent ?? '[]') as PasteBlock[],
  }
}

describe('ChatInput textarea paste provenance', () => {
  it('keeps a colliding marker pasted before the pill literal and moves the existing block', async () => {
    const initial = `before ${TOKEN}`
    renderWithProviders(<Host initial={initial} />)

    pasteLiteralAt(0)

    await waitFor(() => {
      const { value, blocks } = hostPair()
      expect(value.startsWith(TOKEN)).toBe(true)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].seq).not.toBe(BLOCK.seq)
      expect(blocks[0].id).not.toBe(BLOCK.id)
      expect(value).toBe(`${TOKEN}before ${formatToken(blocks[0])}`)
      expect(expandAll(value, blocks)).toBe(`${TOKEN}before ${BLOCK.content}`)
    })
  })

  it('keeps a colliding marker pasted after the pill literal inert', async () => {
    const initial = `${TOKEN} after `
    renderWithProviders(<Host initial={initial} />)

    pasteLiteralAt(initial.length)

    await waitFor(() => {
      const { value, blocks } = hostPair()
      expect(expandAll(value, blocks)).toBe(`${BLOCK.content} after ${TOKEN}`)
    })
  })

  it('drops the displaced block when the literal is pasted over the existing marker', async () => {
    // Selecting exactly the marker's byte range and pasting the same literal
    // over it deletes the only backed occurrence. The moved block is orphaned,
    // the prune effect drops it, and the pasted literal stays inert text.
    const initial = `before ${TOKEN} after`
    renderWithProviders(<Host initial={initial} />)

    const start = 'before '.length
    pasteLiteralAt(start, start + TOKEN.length)

    await waitFor(() => {
      const { blocks } = hostPair()
      expect(blocks).toHaveLength(0)
    })
    const { value, blocks } = hostPair()
    expect(value).toBe(`before ${TOKEN} after`)
    expect(blocks.some(b => b.seq === 1)).toBe(false)
    expect(expandAll(value, blocks)).toBe(`before ${TOKEN} after`)
  })

  it('keeps two pasted marker copies inert and expands only the real pill', async () => {
    const initial = `before ${TOKEN}`
    renderWithProviders(<Host initial={initial} />)

    pasteLiteralAt(0, 0, `${TOKEN} ${TOKEN}`)

    await waitFor(() => {
      const { blocks } = hostPair()
      expect(blocks).toHaveLength(1)
    })
    const { value, blocks } = hostPair()
    expect(blocks[0].seq).not.toBe(BLOCK.seq)
    expect(blocks[0].id).not.toBe(BLOCK.id)
    // The two pasted copies stay literal; only the real pill's marker expands.
    expect(value).toBe(`${TOKEN} ${TOKEN}before ${formatToken(blocks[0])}`)
    expect(expandAll(value, blocks)).toBe(`${TOKEN} ${TOKEN}before ${BLOCK.content}`)
  })
})
