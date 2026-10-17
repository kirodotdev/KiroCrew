/**
 * One keystroke must parse the edited run once, not once per text node.
 *
 * `$rewriteRun` leaves every segment node it touches dirty, and Lexical runs the
 * TextNode transform again for each of them. Each of those runs re-joined and
 * re-parsed the same text before finding it already matched, so a long styled
 * line multiplied the parser's work by its node count on every key.
 */
import { act, render, waitFor } from '@testing-library/react'
import { createRef, useState } from 'react'
import { $getRoot, CONTROLLED_TEXT_INSERTION_COMMAND, type LexicalEditor } from 'lexical'
import { describe, expect, it, vi } from 'vitest'

const parseCalls: string[] = []
vi.mock('../components/composerInlineMarkdown', async importOriginal => {
  const actual = await importOriginal<typeof import('../components/composerInlineMarkdown')>()
  return {
    ...actual,
    parseInlineMarkdown: (text: string) => {
      parseCalls.push(text)
      return actual.parseInlineMarkdown(text)
    },
  }
})

import LexicalComposerInput from '../components/LexicalComposerInput'

function Host({ initial, editorRef }: { initial: string; editorRef: React.RefObject<LexicalEditor | null> }) {
  const [value, setValue] = useState(initial)
  return (
    <LexicalComposerInput
      value={value}
      blocks={[]}
      onChange={setValue}
      onBlocksChange={() => {}}
      onSend={vi.fn()}
      ariaLabel="Message input"
      placeholder="Write a message"
      inlineMarkdown
      editorRef={editorRef}
    />
  )
}

describe('InlineMarkdownPlugin parse cost', () => {
  it('parses once when a closing marker restyles a run of many nodes', async () => {
    const editorRef = createRef<LexicalEditor>()
    render(<Host initial={'**a** _b_ ~~c~~ `d` **e** _f_ ~~g~~ `h` **i*'} editorRef={editorRef} />)
    await waitFor(() => expect(editorRef.current).not.toBeNull())
    const editor = editorRef.current!
    await new Promise<void>(resolve => setTimeout(resolve, 0))

    parseCalls.length = 0
    act(() => { editor.update(() => $getRoot().selectEnd(), { discrete: true }) })
    act(() => { editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, '*') })
    await new Promise<void>(resolve => setTimeout(resolve, 0))

    const typed = parseCalls.filter(text => text.endsWith('**i**'))
    expect(typed.length).toBeGreaterThan(0)
    expect(new Set(typed).size).toBe(1)
    expect(typed.length).toBe(1)
  })
})
