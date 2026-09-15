import { useEffect } from 'react'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { $nodesOfType } from 'lexical'
import { makePasteId } from '../../utils/pasteTokens'
import { PasteBlockNode } from '../nodes/PasteBlockNode'

/**
 * Invariant: no two paste pills share a seq.
 *
 * A marker resolves to its block by seq alone (`findTokenRanges`), so two nodes
 * carrying the same seq collapse onto ONE block on expansion: `expandAll`
 * writes the last block into both markers, and once one twin has been edited
 * the other's content is silently missing from the submitted prompt.
 *
 * Values coming FROM the host are canonicalised before they reach the tree
 * (`splitDuplicateMarkers` in `LexicalComposerInput`), because a rewrite of the
 * initial tree would never reach the host — OnChangePlugin reports neither the
 * initial commit nor a `history-merge` update. This transform guards the other
 * producer: a pill created INSIDE the editor with a seq that is already taken
 * (`importJSON` keeps id + seq). Rule: the node created first keeps its seq;
 * the newcomer gets a fresh identity (seq = max in the tree + 1, fresh id) and
 * its marker text follows (`getTextContent()` derives from the seq); the host
 * receives the rewritten value + blocks through the ordinary OnChange.
 */

/** Transform body, exported for the headless unit test. */
export function $reseqDuplicatePill(node: PasteBlockNode): void {
  const seq = node.getSeq()
  let max = 0
  let first: PasteBlockNode | undefined
  // `$nodesOfType` walks the node map in creation order, so `first` is the pill
  // that held this seq before `node` existed.
  for (const other of $nodesOfType(PasteBlockNode)) {
    const otherSeq = other.getSeq()
    if (otherSeq > max) max = otherSeq
    if (otherSeq === seq && first === undefined) first = other
  }
  if (first === undefined || first.is(node)) return
  node.setIdentity(makePasteId(), max + 1)
}

export default function PasteSeqInvariantPlugin() {
  const [editor] = useLexicalComposerContext()
  useEffect(() => editor.registerNodeTransform(PasteBlockNode, $reseqDuplicatePill), [editor])
  return null
}
