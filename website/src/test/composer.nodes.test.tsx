import { describe, it, expect } from 'vitest'
import { createHeadlessEditor } from '@lexical/headless'
import { $getRoot, $createParagraphNode, type LexicalEditor } from 'lexical'

import {
  PasteBlockNode,
  $createPasteBlockNode,
  $isPasteBlockNode,
} from '../composer/nodes/PasteBlockNode'
import { DropGapNode, $createDropGapNode, $isDropGapNode } from '../composer/nodes/DropGapNode'
import { formatToken } from '../utils/pasteTokens'

function makeEditor(): LexicalEditor {
  return createHeadlessEditor({
    namespace: 'test',
    nodes: [PasteBlockNode, DropGapNode],
    onError: (e) => {
      throw e
    },
  })
}

describe('PasteBlockNode', () => {
  it('getTextContent() returns the literal token', () => {
    const editor = makeEditor()
    editor.update(
      () => {
        const node = $createPasteBlockNode({ seq: 5, lines: 12, content: 'hello\nworld' })
        expect(node.getTextContent()).toBe(formatToken({ id: '', seq: 5, lines: 12, content: '' }))
        expect(node.getTextContent()).toBe('[ Paste #5 · 12 lines ]')
      },
      { discrete: true },
    )
  })

  it('carries seq/lines/content and exposes getters', () => {
    const editor = makeEditor()
    editor.update(
      () => {
        const node = $createPasteBlockNode({ seq: 2, lines: 4, content: 'abc' })
        expect(node.getSeq()).toBe(2)
        expect(node.getLines()).toBe(4)
        expect(node.getContent()).toBe('abc')
        expect($isPasteBlockNode(node)).toBe(true)
      },
      { discrete: true },
    )
  })

  it('JSON round-trips the three fields', () => {
    const editor = makeEditor()
    editor.update(
      () => {
        const root = $getRoot()
        root.clear()
        const p = $createParagraphNode()
        p.append($createPasteBlockNode({ seq: 7, lines: 9, content: 'line1\nline2\nline3' }))
        root.append(p)
      },
      { discrete: true },
    )
    const json = editor.getEditorState().toJSON()
    // Reconstruct into a fresh editor and read the node back.
    const editor2 = makeEditor()
    const state2 = editor2.parseEditorState(JSON.stringify(json))
    editor2.setEditorState(state2)
    editor2.read(() => {
      const nodes = $getRoot().getAllTextNodes()
      // Not a text node — walk children instead.
      const para = $getRoot().getFirstChild()
      expect(para).toBeTruthy()
      const child = (para as ReturnType<typeof $createParagraphNode>).getFirstChild()
      expect($isPasteBlockNode(child)).toBe(true)
      if ($isPasteBlockNode(child)) {
        expect(child.getSeq()).toBe(7)
        expect(child.getLines()).toBe(9)
        expect(child.getContent()).toBe('line1\nline2\nline3')
        expect(child.getTextContent()).toBe('[ Paste #7 · 9 lines ]')
      }
      // silence unused
      void nodes
    })
  })

  it('setData mutates lines + content, changing the serialized token', () => {
    const editor = makeEditor()
    let key = ''
    editor.update(
      () => {
        const root = $getRoot()
        root.clear()
        const p = $createParagraphNode()
        const node = $createPasteBlockNode({ seq: 1, lines: 3, content: 'a\nb\nc' })
        key = node.getKey()
        p.append(node)
        root.append(p)
      },
      { discrete: true },
    )
    editor.update(
      () => {
        const para = $getRoot().getFirstChild()
        const child = (para as ReturnType<typeof $createParagraphNode>).getFirstChild()
        if ($isPasteBlockNode(child)) child.setData(5, 'a\nb\nc\nd\ne')
      },
      { discrete: true },
    )
    editor.read(() => {
      const para = $getRoot().getFirstChild()
      const child = (para as ReturnType<typeof $createParagraphNode>).getFirstChild()
      if ($isPasteBlockNode(child)) {
        expect(child.getLines()).toBe(5)
        expect(child.getTextContent()).toBe('[ Paste #1 · 5 lines ]')
      }
      void key
    })
  })
})

describe('DropGapNode', () => {
  it('serializes to empty text and is identified by $isDropGapNode', () => {
    const editor = makeEditor()
    editor.update(
      () => {
        const node = $createDropGapNode()
        expect(node.getTextContent()).toBe('')
        expect($isDropGapNode(node)).toBe(true)
        expect(node.isInline()).toBe(true)
        expect(node.isKeyboardSelectable()).toBe(false)
      },
      { discrete: true },
    )
  })

  it('JSON round-trips as a drop-gap node', () => {
    const editor = makeEditor()
    editor.update(
      () => {
        const root = $getRoot()
        root.clear()
        const p = $createParagraphNode()
        p.append($createDropGapNode())
        root.append(p)
      },
      { discrete: true },
    )
    const json = JSON.stringify(editor.getEditorState().toJSON())
    const editor2 = makeEditor()
    editor2.setEditorState(editor2.parseEditorState(json))
    editor2.read(() => {
      const para = $getRoot().getFirstChild()
      const child = (para as ReturnType<typeof $createParagraphNode>).getFirstChild()
      expect($isDropGapNode(child)).toBe(true)
    })
  })
})
