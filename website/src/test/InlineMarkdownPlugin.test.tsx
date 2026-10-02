import { act, render, screen, waitFor } from '@testing-library/react'
import { createRef, useState } from 'react'
import type { RefObject } from 'react'
import {
  $getRoot,
  $getSelection,
  $isRangeSelection,
  $isTextNode,
  $setCompositionKey,
  CAN_UNDO_COMMAND,
  COMMAND_PRIORITY_LOW,
  CONTROLLED_TEXT_INSERTION_COMMAND,
  UNDO_COMMAND,
  type LexicalEditor,
} from 'lexical'
import { describe, expect, it, vi } from 'vitest'
import LexicalComposerInput from '../components/LexicalComposerInput'
import { INLINE_MARKER_STYLE } from '../components/InlineMarkdownPlugin'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'

const block: PasteBlock = { id: 'paste-1', seq: 1, lines: 4, content: 'alpha\nbeta\ngamma\ndelta' }

function Host({
  initial = '',
  initialBlocks = [] as PasteBlock[],
  inlineMarkdown = true,
  editorRef,
}: {
  initial?: string
  initialBlocks?: PasteBlock[]
  inlineMarkdown?: boolean
  editorRef?: RefObject<LexicalEditor | null>
}) {
  const [value, setValue] = useState(initial)
  const [blocks, setBlocks] = useState(initialBlocks)
  return (
    <>
      <LexicalComposerInput
        value={value}
        blocks={blocks}
        onChange={setValue}
        onBlocksChange={setBlocks}
        onSend={vi.fn()}
        ariaLabel="Message input"
        placeholder="Write a message"
        inlineMarkdown={inlineMarkdown}
        editorRef={editorRef}
      />
      <output data-testid="value">{value}</output>
    </>
  )
}

async function mount(props: Parameters<typeof Host>[0] = {}) {
  const editorRef = createRef<LexicalEditor>()
  const view = render(<Host {...props} editorRef={editorRef} />)
  await waitFor(() => expect(editorRef.current).not.toBeNull())
  return { editor: editorRef.current!, view, editorRef }
}

async function typeAtEnd(editor: LexicalEditor, text: string) {
  for (const ch of text) {
    act(() => { editor.update(() => $getRoot().selectEnd(), { discrete: true }) })
    act(() => { editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, ch) })
  }
  await new Promise<void>(resolve => setTimeout(resolve, 0))
}

/** Every text node in the editor as [text, format, isMarker]. */
function nodes(editor: LexicalEditor): Array<[string, number, boolean]> {
  const out: Array<[string, number, boolean]> = []
  editor.getEditorState().read(() => {
    for (const node of $getRoot().getAllTextNodes()) {
      if ($isTextNode(node)) out.push([node.getTextContent(), node.getFormat(), node.getStyle() === INLINE_MARKER_STYLE])
    }
  })
  return out
}

const editorText = (editor: LexicalEditor) => editor.getEditorState().read(() => $getRoot().getTextContent())
const textbox = () => screen.getByRole('textbox')

describe('InlineMarkdownPlugin', () => {
  it('styles an existing draft in place and leaves its text untouched', async () => {
    const { editor } = await mount({ initial: 'say **hi** and *you*, ~~no~~ `x`' })
    await waitFor(() => expect(textbox().querySelector('strong')).not.toBeNull())
    expect(textbox().querySelector('strong')).toHaveTextContent('hi')
    expect(textbox().querySelector('.font-bold')).toHaveTextContent('hi')
    expect(textbox().querySelector('.italic')).toHaveTextContent('you')
    expect(textbox().querySelector('.line-through')).toHaveTextContent('no')
    expect(textbox().querySelector('code')).toHaveTextContent('x')
    expect(editorText(editor)).toBe('say **hi** and *you*, ~~no~~ `x`')
    expect(screen.getByTestId('value').textContent).toBe('say **hi** and *you*, ~~no~~ `x`')
    // Markers stay in the DOM, dimmed.
    const markers = Array.from(textbox().querySelectorAll<HTMLElement>('[style]'))
      .filter(el => el.style.color === 'var(--muted)').map(el => el.textContent)
    expect(markers).toEqual(['**', '**', '*', '*', '~~', '~~', '`', '`'])
  })

  it('styles bold as it is typed and reports exactly the typed markdown', async () => {
    const { editor } = await mount()
    await typeAtEnd(editor, '**bold**')
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('**bold**'))
    expect(nodes(editor)).toEqual([['**', 0, true], ['bold', 1, false], ['**', 0, true]])
    await typeAtEnd(editor, ' tail')
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('**bold** tail'))
    expect(nodes(editor)).toEqual([['**', 0, true], ['bold', 1, false], ['**', 0, true], [' tail', 0, false]])
  })

  it('keeps the caret where it was when a closing marker restyles the run', async () => {
    const { editor } = await mount()
    await typeAtEnd(editor, '*a')
    act(() => { editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, '*') })
    act(() => { editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, 'b') })
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('*a*b'))
    expect(nodes(editor)).toEqual([['*', 0, true], ['a', 2, false], ['*', 0, true], ['b', 0, false]])
    editor.getEditorState().read(() => {
      const selection = $getSelection()
      expect($isRangeSelection(selection) && selection.isCollapsed()).toBe(true)
      if ($isRangeSelection(selection)) {
        expect(selection.anchor.getNode().getTextContent()).toBe('b')
        expect(selection.anchor.offset).toBe(1)
      }
    })
  })

  it('never drops or changes an unmatched or literal marker', async () => {
    for (const text of ['**', '2 * 3', '**open', 'snake_case_name', '\\*lit\\*', '``x``', '***both***', '`a **b** c`']) {
      const { editor, view } = await mount({ initial: text })
      await waitFor(() => expect(editorText(editor)).toBe(text))
      expect(nodes(editor).map(([t]) => t).join('')).toBe(text)
      view.unmount()
    }
  })

  it('does not style across a paste chip', async () => {
    const initial = `**a ${formatToken(block)} b**`
    const { editor } = await mount({ initial, initialBlocks: [block] })
    expect(await screen.findByTestId('paste-token-1')).toBeInTheDocument()
    await new Promise<void>(resolve => setTimeout(resolve, 0))
    expect(nodes(editor).every(([, format, marker]) => format === 0 && !marker)).toBe(true)
    expect(editorText(editor)).toBe(initial)
  })

  it('styles nothing when disabled, and clears styling when turned off', async () => {
    const off = await mount({ initial: '**x**', inlineMarkdown: false })
    expect(off.editor && textbox().querySelector('strong')).toBeNull()
    expect(nodes(off.editor)).toEqual([['**x**', 0, false]])
    off.view.unmount()

    const { editor, view, editorRef } = await mount({ initial: '**x**' })
    await waitFor(() => expect(textbox().querySelector('strong')).not.toBeNull())
    view.rerender(<Host initial="**x**" inlineMarkdown={false} editorRef={editorRef} />)
    await waitFor(() => expect(textbox().querySelector('strong')).toBeNull())
    expect(nodes(editor)).toEqual([['**x**', 0, false]])
  })

  it('undo returns to the earlier text with its own styling', async () => {
    const { editor } = await mount()
    await typeAtEnd(editor, '*a*')
    await waitFor(() => expect(textbox().querySelector('em, .italic')).not.toBeNull())
    act(() => { editor.dispatchCommand(UNDO_COMMAND, undefined) })
    await waitFor(() => expect(screen.getByTestId('value').textContent).not.toBe('*a*'))
    const text = screen.getByTestId('value').textContent ?? ''
    expect('*a*'.startsWith(text)).toBe(true)
    expect(nodes(editor).every(([, format]) => format === 0)).toBe(true)
  })

  it('turning styling on over an edited draft adds no undo step of its own', async () => {
    const { editor, view, editorRef } = await mount({ initial: 'say **hi**', inlineMarkdown: false })
    let canUndo: boolean | null = null
    const off = editor.registerCommand(CAN_UNDO_COMMAND, payload => { canUndo = payload; return false }, COMMAND_PRIORITY_LOW)
    await typeAtEnd(editor, 'z')
    await waitFor(() => expect(canUndo).toBe(true))
    view.rerender(<Host initial="say **hi**" inlineMarkdown editorRef={editorRef} />)
    await waitFor(() => expect(textbox().querySelector('strong')).not.toBeNull())
    act(() => { editor.dispatchCommand(UNDO_COMMAND, undefined) })
    // One undo takes back the typed character, not the restyle.
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('say **hi**'))
    await waitFor(() => expect(canUndo).toBe(false))
    off()
  })

  it('leaves a node alone while an IME is composing into it', async () => {
    const { editor } = await mount({ initial: 'x' })
    act(() => {
      editor.update(() => {
        const node = $getRoot().getAllTextNodes()[0]
        $setCompositionKey(node.getKey())
        node.setTextContent('**x**')
      }, { discrete: true })
    })
    expect(nodes(editor)).toEqual([['**x**', 0, false]])
    act(() => {
      editor.update(() => {
        $setCompositionKey(null)
        $getRoot().getAllTextNodes()[0].markDirty()
      }, { discrete: true })
    })
    expect(nodes(editor)).toEqual([['**', 0, true], ['x', 1, false], ['**', 0, true]])
  })
})
