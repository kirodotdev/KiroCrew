import { describe, it, expect, afterEach } from 'vitest'
import { act, render, cleanup } from '@testing-library/react'
import { LexicalComposer } from '@lexical/react/LexicalComposer'
import { PlainTextPlugin } from '@lexical/react/LexicalPlainTextPlugin'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { LexicalErrorBoundary } from '@lexical/react/LexicalErrorBoundary'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  $createNodeSelection,
  $createParagraphNode,
  $createRangeSelection,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isRangeSelection,
  $isTextNode,
  $setSelection,
  KEY_ARROW_LEFT_COMMAND,
  KEY_ARROW_RIGHT_COMMAND,
  KEY_BACKSPACE_COMMAND,
  KEY_DELETE_COMMAND,
  KEY_DOWN_COMMAND,
  KEY_ENTER_COMMAND,
  type LexicalEditor,
} from 'lexical'

import PillKeyboardPlugin from '../composer/plugins/PillKeyboardPlugin'
import { PasteBlockNode, $createPasteBlockNode, $isPasteBlockNode } from '../composer/nodes/PasteBlockNode'

/**
 * The keyboard contract of an atomic pill, driven through Lexical's command bus
 * exactly as the editor dispatches it: Backspace/Delete remove a pill in ONE
 * keystroke when it is node-selected or when the caret sits right next to it
 * (and only then), ←/→ step the caret out of a node-selected pill, a printable
 * key never replaces a node-selected pill, and Enter opens its preview.
 *
 * Seed layout (one paragraph): `'AAAA ' + pill#1 + ' BBBB' + pill#2`.
 */

let editorRef: LexicalEditor
function CaptureEditor() {
  const [editor] = useLexicalComposerContext()
  editorRef = editor
  return null
}

function Harness() {
  return (
    <LexicalComposer
      initialConfig={{
        namespace: 'pill-keyboard-test',
        nodes: [PasteBlockNode],
        onError: (e) => { throw e },
        theme: { paragraph: 'm-0' },
      }}
    >
      <PlainTextPlugin
        contentEditable={<ContentEditable data-composer-input="" />}
        placeholder={null}
        ErrorBoundary={LexicalErrorBoundary}
      />
      <PillKeyboardPlugin />
      <CaptureEditor />
    </LexicalComposer>
  )
}

function seed(): { pill1: string; pill2: string; aaaaKey: string; bbbbKey: string; paraKey: string } {
  const keys = { pill1: '', pill2: '', aaaaKey: '', bbbbKey: '', paraKey: '' }
  act(() => {
    editorRef.update(() => {
      const root = $getRoot()
      root.clear()
      const p = $createParagraphNode()
      const a = $createTextNode('AAAA ')
      const b = $createTextNode(' BBBB')
      const n1 = $createPasteBlockNode({ seq: 1, lines: 10, content: 'one' })
      const n2 = $createPasteBlockNode({ seq: 2, lines: 20, content: 'two' })
      p.append(a, n1, b, n2)
      root.append(p)
      keys.pill1 = n1.getKey(); keys.pill2 = n2.getKey(); keys.aaaaKey = a.getKey(); keys.bbbbKey = b.getKey(); keys.paraKey = p.getKey()
    }, { discrete: true })
  })
  return keys
}

async function mount(): Promise<void> {
  await act(async () => { render(<Harness />) })
}

/** Order of node types in the first paragraph, e.g. ['text','paste','text','paste']. */
function order(): string[] {
  return editorRef.read(() => {
    const p = $getRoot().getFirstChild()
    if (!p || !('getChildren' in p)) return []
    return (p as unknown as { getChildren(): unknown[] }).getChildren().map((c) => ($isTextNode(c) ? `text:${c.getTextContent()}` : $isPasteBlockNode(c) ? `pill:${c.__seq}` : 'other'))
  })
}

function selectNodes(...keys: string[]): void {
  act(() => {
    editorRef.update(() => {
      const sel = $createNodeSelection()
      for (const k of keys) sel.add(k)
      $setSelection(sel)
    }, { discrete: true })
  })
}

function selectText(key: string, offset: number): void {
  act(() => {
    editorRef.update(() => {
      const sel = $createRangeSelection()
      sel.anchor.set(key, offset, 'text')
      sel.focus.set(key, offset, 'text')
      $setSelection(sel)
    }, { discrete: true })
  })
}

function selectElement(paraKey: string, childIndex: number): void {
  act(() => {
    editorRef.update(() => {
      const sel = $createRangeSelection()
      sel.anchor.set(paraKey, childIndex, 'element')
      sel.focus.set(paraKey, childIndex, 'element')
      $setSelection(sel)
    }, { discrete: true })
  })
}

function dispatch(command: Parameters<LexicalEditor['dispatchCommand']>[0], event: KeyboardEvent): boolean {
  let handled = false
  act(() => { handled = editorRef.dispatchCommand(command, event) })
  return handled
}

function key(init: KeyboardEventInit = {}): KeyboardEvent {
  return new KeyboardEvent('keydown', { cancelable: true, ...init })
}

/** Where the caret is after a command: `{ key, offset }` of a collapsed RangeSelection, or 'node'. */
function caret(): { key: string; offset: number; type: string } | 'node' | null {
  return editorRef.read(() => {
    const sel = $getSelection()
    if (!sel) return null
    if (!$isRangeSelection(sel)) return 'node'
    return { key: sel.anchor.key, offset: sel.anchor.offset, type: sel.anchor.type }
  })
}

describe('PillKeyboardPlugin', () => {
  afterEach(() => cleanup())

  it('Backspace with a node-selected pill removes exactly that pill (and consumes the key)', async () => {
    await mount()
    const { pill1 } = seed()
    selectNodes(pill1)
    const ev = key()
    expect(dispatch(KEY_BACKSPACE_COMMAND, ev)).toBe(true)
    expect(ev.defaultPrevented).toBe(true)
    expect(order()).toEqual(['text:AAAA  BBBB', 'pill:2'])
  })

  it('Delete with several node-selected pills removes them all', async () => {
    await mount()
    const { pill1, pill2 } = seed()
    selectNodes(pill1, pill2)
    expect(dispatch(KEY_DELETE_COMMAND, key())).toBe(true)
    expect(order()).toEqual(['text:AAAA  BBBB'])
  })

  it('Backspace with the caret at the START of the text right after a pill removes the pill in one keystroke', async () => {
    await mount()
    const { bbbbKey } = seed()
    selectText(bbbbKey, 0)
    expect(dispatch(KEY_BACKSPACE_COMMAND, key())).toBe(true)
    expect(order()).toEqual(['text:AAAA  BBBB', 'pill:2'])
  })

  it('Delete with the caret at the END of the text right before a pill removes the pill', async () => {
    await mount()
    const { aaaaKey } = seed()
    selectText(aaaaKey, 'AAAA '.length)
    expect(dispatch(KEY_DELETE_COMMAND, key())).toBe(true)
    expect(order()).toEqual(['text:AAAA  BBBB', 'pill:2'])
  })

  it.each([
    ['Backspace', KEY_BACKSPACE_COMMAND, 4],
    ['Delete', KEY_DELETE_COMMAND, 3],
  ] as const)('%s command from inside a focused chip claims the key without touching the stale caret', async (keyName, command, caretOffset) => {
    await mount()
    const { paraKey } = seed()
    selectElement(paraKey, caretOffset)
    const chip = document.querySelector<HTMLElement>('[data-paste-seq="1"]')!
    const ev = key({ key: keyName, bubbles: true })
    Object.defineProperty(ev, 'target', { value: chip })

    expect(dispatch(command, ev)).toBe(true)
    expect(order()).toEqual(['text:AAAA ', 'pill:1', 'text: BBBB', 'pill:2'])
    expect(ev.defaultPrevented).toBe(true)
  })

  it('Backspace on a focused chip removes only that chip, not the pill beside the stale editor caret', async () => {
    await mount()
    const { paraKey } = seed()
    // Keep Lexical's caret after pill #2, then move DOM focus to pill #1.
    selectElement(paraKey, 4)
    const chip = document.querySelector<HTMLElement>('[data-paste-seq="1"]')!
    chip.focus()
    let nativeRootSawKey = false
    editorRef.getRootElement()!.addEventListener('keydown', () => { nativeRootSawKey = true }, { once: true })
    const ev = key({ key: 'Backspace', bubbles: true })
    act(() => { chip.dispatchEvent(ev) })

    expect(nativeRootSawKey).toBe(false)
    expect(order()).toEqual(['text:AAAA  BBBB', 'pill:2'])
    expect(ev.defaultPrevented).toBe(true)
  })

  it('Delete on a focused chip removes only that chip, not the pill beside the stale editor caret', async () => {
    await mount()
    const { paraKey } = seed()
    // Keep Lexical's caret before pill #2, then move DOM focus to pill #1.
    selectElement(paraKey, 3)
    const chip = document.querySelector<HTMLElement>('[data-paste-seq="1"]')!
    chip.focus()
    let nativeRootSawKey = false
    editorRef.getRootElement()!.addEventListener('keydown', () => { nativeRootSawKey = true }, { once: true })
    const ev = key({ key: 'Delete', bubbles: true })
    act(() => { chip.dispatchEvent(ev) })

    expect(nativeRootSawKey).toBe(false)
    expect(order()).toEqual(['text:AAAA  BBBB', 'pill:2'])
    expect(ev.defaultPrevented).toBe(true)
  })

  it('Backspace/Delete in the MIDDLE of text are left to the editor (nothing removed here)', async () => {
    await mount()
    const { aaaaKey } = seed()
    // `dispatchCommand` reports whether ANY listener handled the key, and
    // PlainTextPlugin handles the fall-through (one character), so the
    // observable contract is the tree: both pills survive.
    selectText(aaaaKey, 2)
    dispatch(KEY_BACKSPACE_COMMAND, key())
    dispatch(KEY_DELETE_COMMAND, key())
    expect(order().filter(o => o.startsWith('pill:'))).toEqual(['pill:1', 'pill:2'])
  })

  it('an element-point caret (paragraph child index) removes the pill on the matching side', async () => {
    await mount()
    const { paraKey } = seed()
    // Caret between pill#1 (index 1) and ' BBBB' (index 2): Backspace takes index 1.
    selectElement(paraKey, 2)
    expect(dispatch(KEY_BACKSPACE_COMMAND, key())).toBe(true)
    expect(order()).toEqual(['text:AAAA  BBBB', 'pill:2'])
    // Caret right before pill#2 (the merged text is index 0, so pill#2 is index 1): Delete takes it.
    selectElement(paraKey, 1)
    expect(dispatch(KEY_DELETE_COMMAND, key())).toBe(true)
    expect(order()).toEqual(['text:AAAA  BBBB'])
  })

  it('← on a node-selected pill parks the caret before it, → after it', async () => {
    await mount()
    const { pill1, aaaaKey, bbbbKey } = seed()
    selectNodes(pill1)
    const left = key()
    expect(dispatch(KEY_ARROW_LEFT_COMMAND, left)).toBe(true)
    expect(left.defaultPrevented).toBe(true)
    expect(caret()).toEqual({ key: aaaaKey, offset: 'AAAA '.length, type: 'text' })
    selectNodes(pill1)
    expect(dispatch(KEY_ARROW_RIGHT_COMMAND, key())).toBe(true)
    expect(caret()).toEqual({ key: bbbbKey, offset: 0, type: 'text' })
  })

  it('arrows with an ordinary caret are not handled here', async () => {
    await mount()
    const { aaaaKey } = seed()
    selectText(aaaaKey, 1)
    dispatch(KEY_ARROW_LEFT_COMMAND, key())
    dispatch(KEY_ARROW_RIGHT_COMMAND, key())
    // Plain-caret arrows are the browser's native move (nothing moves a caret
    // in the test DOM): this plugin must touch neither the caret nor a pill.
    expect(caret()).toEqual({ key: aaaaKey, offset: 1, type: 'text' })
    expect(order()).toEqual(['text:AAAA ', 'pill:1', 'text: BBBB', 'pill:2'])
  })

  it('a printable key on a node-selected pill parks the caret after the pill and lets the key through', async () => {
    await mount()
    const { pill1, bbbbKey } = seed()
    selectNodes(pill1)
    dispatch(KEY_DOWN_COMMAND, key({ key: 'x' }))
    expect(caret()).toEqual({ key: bbbbKey, offset: 0, type: 'text' })
    expect(order()).toEqual(['text:AAAA ', 'pill:1', 'text: BBBB', 'pill:2'])
  })

  it('a chord or a non-printable key on a node-selected pill leaves the selection alone', async () => {
    await mount()
    const { pill1 } = seed()
    selectNodes(pill1)
    dispatch(KEY_DOWN_COMMAND, key({ key: 'x', ctrlKey: true }))
    expect(caret()).toBe('node')
    dispatch(KEY_DOWN_COMMAND, key({ key: 'Shift' }))
    expect(caret()).toBe('node')
  })

  it('Enter on a node-selected pill clicks the chip (opens its preview) and consumes the key; Enter elsewhere is not handled', async () => {
    await mount()
    const { pill1, aaaaKey } = seed()
    const clicked: string[] = []
    editorRef.getRootElement()!.addEventListener('click', (e) => {
      const seq = (e.target as HTMLElement).closest('[data-paste-seq]')?.getAttribute('data-paste-seq')
      if (seq) clicked.push(seq)
    })
    selectNodes(pill1)
    const ev = key({ key: 'Enter' })
    expect(dispatch(KEY_ENTER_COMMAND, ev)).toBe(true)
    expect(ev.defaultPrevented).toBe(true)
    expect(clicked).toEqual(['1'])
    selectText(aaaaKey, 1)
    dispatch(KEY_ENTER_COMMAND, key({ key: 'Enter' }))
    expect(clicked).toEqual(['1'])
  })
})
