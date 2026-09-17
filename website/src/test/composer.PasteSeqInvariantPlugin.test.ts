import { describe, it, expect } from 'vitest'
import { createHeadlessEditor } from '@lexical/headless'
import { $createParagraphNode, $getRoot, $nodesOfType, type LexicalEditor } from 'lexical'

import { PasteBlockNode, $createPasteBlockNode } from '../composer/nodes/PasteBlockNode'
import { $reseqDuplicatePill } from '../composer/plugins/PasteSeqInvariantPlugin'
import type { PasteBlock } from '../utils/pasteTokens'

/**
 * `PasteSeqInvariantPlugin` registers `$reseqDuplicatePill` as a node transform:
 * no two pills may share a seq, because a marker resolves to its block by seq
 * alone and twins collapse onto one block on expansion. The React plugin is a
 * one-line `registerNodeTransform`; the rule itself is tested headless here.
 */
function makeEditor(): LexicalEditor {
  const editor = createHeadlessEditor({
    namespace: 'test',
    nodes: [PasteBlockNode],
    onError: (e) => { throw e },
  })
  editor.registerNodeTransform(PasteBlockNode, $reseqDuplicatePill)
  return editor
}

function $append(blocks: PasteBlock[]): void {
  const paragraph = $createParagraphNode()
  $getRoot().append(paragraph)
  for (const b of blocks) paragraph.append($createPasteBlockNode(b))
}

function pills(editor: LexicalEditor): PasteBlock[] {
  let out: PasteBlock[] = []
  editor.getEditorState().read(() => { out = $nodesOfType(PasteBlockNode).map(n => n.getBlock()) })
  return out
}

const a: PasteBlock = { id: 'A', seq: 1, lines: 2, content: 'a\nb' }

describe('PasteSeqInvariantPlugin — $reseqDuplicatePill', () => {
  it('leaves pills with distinct seqs untouched (ids and seqs stable)', () => {
    const editor = makeEditor()
    const b: PasteBlock = { id: 'B', seq: 2, lines: 1, content: 'b' }
    editor.update(() => $append([a, b]), { discrete: true })
    expect(pills(editor)).toEqual([a, b])
  })

  it('the pill created first keeps its seq; a later twin gets a fresh seq past the max and a fresh id', () => {
    const editor = makeEditor()
    const far: PasteBlock = { id: 'F', seq: 5, lines: 1, content: 'far' }
    // Two nodes minted from the SAME block (what a duplicated marker rehydrates to).
    editor.update(() => $append([a, a, far]), { discrete: true })
    const [first, twin, unrelated] = pills(editor)
    expect(first).toEqual(a)
    expect(unrelated).toEqual(far)
    expect(twin).toEqual(expect.objectContaining({ lines: a.lines, content: a.content }))
    expect(twin.seq).toBe(6)
    expect(twin.id).not.toBe(a.id)
  })

  it('three twins end up with three distinct seqs; the marker text follows the seq', () => {
    const editor = makeEditor()
    editor.update(() => $append([a, a, a]), { discrete: true })
    const result = pills(editor)
    expect(result.map(p => p.seq).sort((x, y) => x - y)).toEqual([1, 2, 3])
    expect(new Set(result.map(p => p.id)).size).toBe(3)
    editor.getEditorState().read(() => {
      expect($getRoot().getTextContent()).toBe('[ Paste #1 · 2 lines ][ Paste #2 · 2 lines ][ Paste #3 · 2 lines ]')
    })
  })

  it('a twin added later (an importJSON / paste of an existing pill) is re-sequenced on arrival', () => {
    const editor = makeEditor()
    editor.update(() => $append([a]), { discrete: true })
    editor.update(() => {
      const paragraph = $getRoot().getFirstChild()
      if (paragraph && 'append' in paragraph) (paragraph as ReturnType<typeof $createParagraphNode>).append($createPasteBlockNode(a))
    }, { discrete: true })
    const [first, twin] = pills(editor)
    expect(first).toEqual(a)
    expect(twin.seq).toBe(2)
    expect(twin.id).not.toBe(a.id)
  })
})
