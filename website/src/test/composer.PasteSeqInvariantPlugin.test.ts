import { describe, it, expect } from 'vitest'
import { createHeadlessEditor } from '@lexical/headless'
import {
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $nodesOfType,
  TextNode,
  type LexicalEditor,
} from 'lexical'

import { PasteBlockNode, $createPasteBlockNode } from '../composer/nodes/PasteBlockNode'
import {
  $reseqDuplicatePill,
  $reseqPillsCollidingWithText,
} from '../composer/plugins/PasteSeqInvariantPlugin'
import { expandAll, type PasteBlock } from '../utils/pasteTokens'

/**
 * `PasteSeqInvariantPlugin` registers `$reseqDuplicatePill` as a node transform:
 * no two pills may share a seq, because a marker resolves to its block by seq
 * alone and twins collapse onto one block on expansion. The React plugin is a
 * one-line `registerNodeTransform`; the rule itself is tested headless here.
 */
function makeEditor(registerTransforms = true): LexicalEditor {
  const editor = createHeadlessEditor({
    namespace: 'test',
    nodes: [PasteBlockNode],
    onError: (e) => { throw e },
  })
  if (registerTransforms) {
    editor.registerNodeTransform(PasteBlockNode, $reseqDuplicatePill)
    editor.registerNodeTransform(TextNode, $reseqPillsCollidingWithText)
  }
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

function rootText(editor: LexicalEditor): string {
  let out = ''
  editor.getEditorState().read(() => { out = $getRoot().getTextContent() })
  return out
}

function textNodes(editor: LexicalEditor): string[] {
  let out: string[] = []
  editor.getEditorState().read(() => { out = $nodesOfType(TextNode).map(node => node.getTextContent()) })
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

  it('a fresh seq skips a marker that exists only as TEXT in the document (round-10 finding)', () => {
    // A literal `[ Paste #2 · 9 lines ]` typed or pasted as plain text has no
    // pill behind it and expands to itself. The twin must not be minted at 2, or
    // the literal would resolve to the twin's block in the sent value.
    const editor = makeEditor()
    editor.update(() => {
      const paragraph = $createParagraphNode()
      $getRoot().append(paragraph)
      paragraph.append($createPasteBlockNode(a))
      paragraph.append($createTextNode(' typed: [ Paste #2 · 9 lines ] '))
      paragraph.append($createPasteBlockNode(a))
    }, { discrete: true })
    const [first, twin] = pills(editor)
    expect(first).toEqual(a)
    expect(twin.seq).toBe(3)
    editor.getEditorState().read(() => {
      expect($getRoot().getTextContent()).toBe('[ Paste #1 · 2 lines ] typed: [ Paste #2 · 9 lines ] [ Paste #3 · 2 lines ]')
    })
  })

  it('re-sequences a pill when a same-seq literal marker is before it', () => {
    const editor = makeEditor()
    const literal = '[ Paste #1 · 2 lines ]'
    editor.update(() => {
      const paragraph = $createParagraphNode()
      $getRoot().append(paragraph)
      paragraph.append($createTextNode(`${literal} before `), $createPasteBlockNode(a))
    }, { discrete: true })

    const [pill] = pills(editor)
    expect(pill).toEqual(expect.objectContaining({ seq: 2, lines: a.lines, content: a.content }))
    expect(pill.id).not.toBe(a.id)
    expect(rootText(editor)).toBe(`${literal} before [ Paste #2 · 2 lines ]`)
    expect(expandAll(rootText(editor), [pill])).toBe(`${literal} before ${a.content}`)
    expect(textNodes(editor)).toEqual([`${literal} before `])
  })

  it('re-sequences a pill when a same-seq literal marker is after it', () => {
    const editor = makeEditor()
    const literal = '[ Paste #1 · 2 lines ]'
    editor.update(() => {
      const paragraph = $createParagraphNode()
      $getRoot().append(paragraph)
      paragraph.append($createPasteBlockNode(a), $createTextNode(` after ${literal}`))
    }, { discrete: true })

    const [pill] = pills(editor)
    expect(pill).toEqual(expect.objectContaining({ seq: 2, lines: a.lines, content: a.content }))
    expect(rootText(editor)).toBe(`[ Paste #2 · 2 lines ] after ${literal}`)
    expect(expandAll(rootText(editor), [pill])).toBe(`${a.content} after ${literal}`)
    expect(textNodes(editor)).toEqual([` after ${literal}`])
  })

  it('re-sequences an existing pill when a TextNode is edited to add its marker', () => {
    const editor = makeEditor()
    editor.update(() => {
      const paragraph = $createParagraphNode()
      $getRoot().append(paragraph)
      paragraph.append($createPasteBlockNode(a), $createTextNode(' tail'))
    }, { discrete: true })
    expect(pills(editor)).toEqual([a])

    editor.update(() => {
      $nodesOfType(TextNode)[0]?.setTextContent(' tail [ Paste #1 · 2 lines ]')
    }, { discrete: true })

    const [pill] = pills(editor)
    expect(pill.seq).toBe(2)
    expect(pill.id).not.toBe(a.id)
    expect(rootText(editor)).toBe('[ Paste #2 · 2 lines ] tail [ Paste #1 · 2 lines ]')
    expect(expandAll(rootText(editor), [pill])).toBe(`${a.content} tail [ Paste #1 · 2 lines ]`)
  })

  it('re-sequences a pill moved behind a same-seq literal', () => {
    const editor = makeEditor(false)
    editor.update(() => {
      const paragraph = $createParagraphNode()
      $getRoot().append(paragraph)
      paragraph.append($createPasteBlockNode(a), $createTextNode(' before [ Paste #1 · 2 lines ]'))
    }, { discrete: true })
    editor.registerNodeTransform(PasteBlockNode, $reseqDuplicatePill)
    editor.registerNodeTransform(TextNode, $reseqPillsCollidingWithText)

    editor.update(() => {
      const paragraph = $getRoot().getFirstChildOrThrow()
      const pill = $nodesOfType(PasteBlockNode)[0]
      paragraph.append(pill)
    }, { discrete: true })

    const [pill] = pills(editor)
    expect(pill.seq).toBe(2)
    expect(rootText(editor)).toBe(' before [ Paste #1 · 2 lines ][ Paste #2 · 2 lines ]')
    expect(expandAll(rootText(editor), [pill])).toBe(` before [ Paste #1 · 2 lines ]${a.content}`)
  })

  it('is idempotent after a literal collision has been repaired', () => {
    const editor = makeEditor()
    editor.update(() => {
      const paragraph = $createParagraphNode()
      $getRoot().append(paragraph)
      paragraph.append($createTextNode('[ Paste #1 · 2 lines ] before '), $createPasteBlockNode(a))
    }, { discrete: true })
    const repaired = pills(editor)[0]

    editor.update(() => {
      const literal = $nodesOfType(TextNode)[0]
      literal?.setTextContent(literal.getTextContent())
    }, { discrete: true })

    expect(pills(editor)).toEqual([repaired])
    expect(rootText(editor)).toBe('[ Paste #1 · 2 lines ] before [ Paste #2 · 2 lines ]')
  })

  it('twins stay distinct when a literal marker in the text sits at the safe-integer limit (round-11 finding)', () => {
    // `max + 1` past 2^53 - 1 does not move, so two twins minted that way would
    // share a seq. The allocator falls back to the first free small integer.
    const editor = makeEditor()
    editor.update(() => {
      const paragraph = $createParagraphNode()
      $getRoot().append(paragraph)
      paragraph.append($createTextNode('[ Paste #9007199254740991 · 1 lines ] '))
      paragraph.append($createPasteBlockNode(a))
      paragraph.append($createPasteBlockNode(a))
      paragraph.append($createPasteBlockNode(a))
    }, { discrete: true })
    const seqs = pills(editor).map(p => p.seq)
    expect(new Set(seqs).size).toBe(3)
    for (const s of seqs) expect(Number.isSafeInteger(s)).toBe(true)
    expect(seqs.sort((x, y) => x - y)).toEqual([1, 2, 3])
  })
})
