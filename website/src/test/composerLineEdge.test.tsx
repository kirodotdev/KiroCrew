import { act, createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createRef, useState } from 'react'
import type { MutableRefObject } from 'react'
import { CONTROLLED_TEXT_INSERTION_COMMAND, type LexicalEditor } from 'lexical'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import LexicalComposerInput from '../components/LexicalComposerInput'
import { lineEdgeForKey, moveDomCaretToLineEdge } from '../components/composerLineEdge'
import type { ComposerControl } from '../components/composerControl'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import { isMacOSPlatform } from '../utils/platform'

const key = (overrides: Partial<KeyboardEvent> = {}) => ({
  key: 'Home',
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  isComposing: false,
  keyCode: 36,
  ...overrides,
})

afterEach(() => vi.restoreAllMocks())

describe('isMacOSPlatform', () => {
  it.each([
    ['MacIntel', 0, true],
    ['MacIntel', 5, false],
    ['iPad', 0, false],
    ['iPhone', 0, false],
    ['Win32', 0, false],
    ['Linux x86_64', 0, false],
  ])('returns %s with %i touch points => %s', (platform, maxTouchPoints, expected) => {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform)
    vi.spyOn(navigator, 'maxTouchPoints', 'get').mockReturnValue(maxTouchPoints)
    expect(isMacOSPlatform()).toBe(expected)
  })
})

describe('lineEdgeForKey', () => {
  it('maps bare Home and End to the line start and end on macOS', () => {
    expect(lineEdgeForKey(key({ key: 'Home' }), true)).toBe('start')
    expect(lineEdgeForKey(key({ key: 'End', keyCode: 35 }), true)).toBe('end')
  })

  it('leaves every platform other than macOS on native behaviour', () => {
    expect(lineEdgeForKey(key({ key: 'Home' }), false)).toBeNull()
    expect(lineEdgeForKey(key({ key: 'End' }), false)).toBeNull()
  })

  it.each(['altKey', 'ctrlKey', 'metaKey', 'shiftKey'] as const)('ignores Home with %s held', modifier => {
    expect(lineEdgeForKey(key({ [modifier]: true }), true)).toBeNull()
  })

  it('ignores keys that belong to an IME composition', () => {
    expect(lineEdgeForKey(key({ isComposing: true }), true)).toBeNull()
    expect(lineEdgeForKey(key({ keyCode: 229 }), true)).toBeNull()
  })

  it('ignores other keys', () => {
    expect(lineEdgeForKey(key({ key: 'ArrowLeft' }), true)).toBeNull()
    expect(lineEdgeForKey(key({ key: 'PageUp' }), true)).toBeNull()
  })
})

// happy-dom has no layout, so `Selection.modify('lineboundary')` is stood in
// for by a stub that moves the caret to the edge of the focused text node —
// the row the real engine would pick for a one-row line. What the DOM tests
// pin is everything around that call: the claim, the Lexical selection sync
// and the keys that must stay native.
function stubLineBoundary() {
  const modify = vi.fn(function (this: Selection, _alter: string, direction: string) {
    const node = this.focusNode
    if (!node) return
    const offset = direction === 'backward' ? 0 : (node.textContent ?? '').length
    this.collapse(node, offset)
  })
  Object.defineProperty(Selection.prototype, 'modify', { value: modify, configurable: true, writable: true })
  return modify
}

function Host({
  initial,
  blocks: initialBlocks = [],
  editorRef,
  controlRef,
}: {
  initial: string
  blocks?: PasteBlock[]
  editorRef: React.RefObject<LexicalEditor | null>
  controlRef: MutableRefObject<ComposerControl | null>
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
        editorRef={editorRef}
        controlRef={controlRef}
      />
      <output data-testid="value">{value}</output>
    </>
  )
}

async function mount(initial: string, caret: number, blocks: PasteBlock[] = []) {
  const editorRef = createRef<LexicalEditor>()
  const controlRef: MutableRefObject<ComposerControl | null> = { current: null }
  render(<Host initial={initial} blocks={blocks} editorRef={editorRef} controlRef={controlRef} />)
  await waitFor(() => expect(controlRef.current).not.toBeNull())
  act(() => controlRef.current!.setSelection(caret, caret, { focus: true }))
  const root = screen.getByRole('textbox', { name: 'Message input' })
  await waitFor(() => expect(window.getSelection()?.focusNode && root.contains(window.getSelection()!.focusNode)).toBe(true))
  return { editor: editorRef.current!, control: controlRef.current!, root }
}

function press(target: Element, init: KeyboardEventInit) {
  const event = createEvent.keyDown(target, init)
  fireEvent(target, event)
  return event
}

describe('MacLineEdgePlugin', () => {
  beforeEach(() => {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue('MacIntel')
    vi.spyOn(navigator, 'maxTouchPoints', 'get').mockReturnValue(0)
  })

  afterEach(() => {
    delete (Selection.prototype as { modify?: unknown }).modify
  })

  it('moves the Lexical selection to the line start on Home and types there', async () => {
    const modify = stubLineBoundary()
    const { editor, control, root } = await mount('hello world', 6)
    const event = press(root, { key: 'Home', keyCode: 36 })
    expect(event.defaultPrevented).toBe(true)
    expect(modify).toHaveBeenCalledWith('move', 'backward', 'lineboundary')
    await waitFor(() => expect(control.getSelection()).toEqual({ start: 0, end: 0 }))
    act(() => { editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, '> ') })
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('> hello world'))
  })

  it('moves the Lexical selection to the line end on End', async () => {
    const modify = stubLineBoundary()
    const { control, root } = await mount('hello world', 2)
    const event = press(root, { key: 'End', keyCode: 35 })
    expect(event.defaultPrevented).toBe(true)
    expect(modify).toHaveBeenCalledWith('move', 'forward', 'lineboundary')
    await waitFor(() => expect(control.getSelection()).toEqual({ start: 11, end: 11 }))
  })

  it.each([
    ['Shift', { shiftKey: true }],
    ['Cmd', { metaKey: true }],
    ['Ctrl', { ctrlKey: true }],
    ['Alt', { altKey: true }],
    ['an IME composition', { isComposing: true }],
  ])('leaves Home native with %s', async (_label, init) => {
    const modify = stubLineBoundary()
    const { control, root } = await mount('hello world', 6)
    const event = press(root, { key: 'Home', keyCode: 36, ...init })
    expect(event.defaultPrevented).toBe(false)
    expect(modify).not.toHaveBeenCalled()
    expect(control.getSelection()).toEqual({ start: 6, end: 6 })
  })

  it('does not claim Home on desktop-mode iPadOS', async () => {
    vi.spyOn(navigator, 'maxTouchPoints', 'get').mockReturnValue(5)
    const modify = stubLineBoundary()
    const { root } = await mount('hello world', 6)
    const event = press(root, { key: 'Home', keyCode: 36 })
    expect(event.defaultPrevented).toBe(false)
    expect(modify).not.toHaveBeenCalled()
  })

  it('does not claim Home on iPad', async () => {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue('iPad')
    const modify = stubLineBoundary()
    const { root } = await mount('hello world', 6)
    const event = press(root, { key: 'Home', keyCode: 36 })
    expect(event.defaultPrevented).toBe(false)
    expect(modify).not.toHaveBeenCalled()
  })

  it('does not claim Home when a chip inside the editor has focus', async () => {
    const modify = stubLineBoundary()
    const block: PasteBlock = { id: 'paste-1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }
    const { root } = await mount(`x ${formatToken(block)} y`, 0, [block])
    const chip = root.querySelector('[data-paste-seq]')
    expect(chip).not.toBeNull()
    const event = press(chip!, { key: 'Home', keyCode: 36 })
    expect(event.defaultPrevented).toBe(false)
    expect(modify).not.toHaveBeenCalled()
  })

  it('falls back to native behaviour when the engine has no Selection.modify', async () => {
    const { control, root } = await mount('hello world', 6)
    expect(moveDomCaretToLineEdge(root, 'start')).toBe(false)
    const event = press(root, { key: 'Home', keyCode: 36 })
    expect(event.defaultPrevented).toBe(false)
    expect(control.getSelection()).toEqual({ start: 6, end: 6 })
  })
})
