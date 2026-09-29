import { useEffect } from 'react'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { $getRoot, $nodesOfType, TextNode } from 'lexical'
import { PASTE_TOKEN_REGEX, allocateSeq, makePasteId } from '../../utils/pasteTokens'
import { PasteBlockNode } from '../nodes/PasteBlockNode'

/**
 * Invariant: a paste pill's seq appears nowhere else in the document text.
 *
 * A marker resolves to its block by seq alone (`findTokenRanges`). A same-seq
 * literal TextNode before a pill therefore steals the block, while one after it
 * can become bound after a drag. Same-seq pill twins can likewise collapse onto
 * one block. The tree knows which occurrences are pills, so this plugin gives
 * every colliding pill a fresh id + seq and leaves literal text inert.
 *
 * Values coming FROM the host are canonicalised before they reach the tree
 * (`splitDuplicateMarkers` in `LexicalComposerInput`). This plugin guards
 * collisions created INSIDE the editor. Pill transforms cover creation, import,
 * and moves. A TextNode transform covers a literal typed or pasted after a pill
 * already exists. The repair is idempotent: once each pill seq occurs exactly
 * once, later transforms make no writes.
 *
 * The identity change reaches the host through OnChange -> `$composerSnapshot`.
 * Both the value and block list change, `sameBlocks` observes the new id/seq,
 * and `lastEmittedRef` prevents controlled sync from restoring the old pair.
 */

/**
 * Repair every colliding seq as one tree transaction.
 *
 * With pill twins only, the first-created pill keeps the seq. If literal text
 * also carries that seq, every pill must move because the literal deliberately
 * keeps its bytes and must remain unbacked.
 */
function $repairPasteSeqInvariants(): void {
  const nodes = $nodesOfType(PasteBlockNode)
  if (nodes.length === 0) return

  const bySeq = new Map<number, PasteBlockNode[]>()
  const used = new Set<number>()
  let max = 0
  for (const node of nodes) {
    const seq = node.getSeq()
    const group = bySeq.get(seq)
    if (group) group.push(node)
    else bySeq.set(seq, [node])
    used.add(seq)
    if (seq > max) max = seq
  }

  const markerCounts = new Map<number, number>()
  PASTE_TOKEN_REGEX.lastIndex = 0
  const text = $getRoot().getTextContent()
  let match: RegExpExecArray | null
  while ((match = PASTE_TOKEN_REGEX.exec(text)) !== null) {
    const seq = Number(match[1])
    markerCounts.set(seq, (markerCounts.get(seq) ?? 0) + 1)
    used.add(seq)
    if (seq > max) max = seq
  }

  for (const [seq, group] of bySeq) {
    const markerCount = markerCounts.get(seq) ?? 0
    const hasLiteral = markerCount > group.length
    const firstToReseq = hasLiteral ? 0 : 1
    if (markerCount <= 1 || firstToReseq >= group.length) continue

    for (let index = firstToReseq; index < group.length; index += 1) {
      const freshSeq = allocateSeq(max, used)
      used.add(freshSeq)
      if (freshSeq > max) max = freshSeq
      group[index].setIdentity(makePasteId(), freshSeq)
    }
  }
}

/** Transform body for a dirty/new/moved pill, exported for headless tests. */
export function $reseqDuplicatePill(_node: PasteBlockNode): void {
  $repairPasteSeqInvariants()
}

/**
 * A literal edit does not dirty an existing pill. Only invoke the whole-tree
 * repair when this dirty TextNode contains a marker whose seq a pill carries.
 */
export function $reseqPillsCollidingWithText(node: TextNode): void {
  const pillSeqs = new Set($nodesOfType(PasteBlockNode).map(pill => pill.getSeq()))
  if (pillSeqs.size === 0) return

  PASTE_TOKEN_REGEX.lastIndex = 0
  const text = node.getTextContent()
  let match: RegExpExecArray | null
  while ((match = PASTE_TOKEN_REGEX.exec(text)) !== null) {
    if (pillSeqs.has(Number(match[1]))) {
      $repairPasteSeqInvariants()
      return
    }
  }
}

export default function PasteSeqInvariantPlugin() {
  const [editor] = useLexicalComposerContext()
  useEffect(() => {
    // Registering a node transform triggers an immediate repair pass over the
    // existing tree, but Lexical tags that pass `history-merge`
    // (markNodesWithTypesAsDirty) and OnChangePlugin deliberately ignores it, so
    // the canonicalised value + blocks would never reach the host. Run an
    // explicit UNTAGGED `editor.update(..., { discrete: true })` repair instead,
    // which OnChange publishes normally. Defer it one microtask so it lands
    // OUTSIDE the initial passive-effect flush (sibling plugins such as
    // ControlledValuePlugin / InteractionPlugin are still mounting on that
    // flush), while keeping the macrotask window in which a user edit or an
    // importJSON paste could arrive before the transforms are armed as small as
    // possible. `queueMicrotask` cannot be cancelled, so the `active` flag is the
    // sole guard: under StrictMode's double invoke the first effect's cleanup
    // sets its own closure's `active = false`, so its microtask no-ops, and the
    // second effect registers.
    let active = true
    let unregisterPill: (() => void) | undefined
    let unregisterText: (() => void) | undefined
    queueMicrotask(() => {
      if (!active) return
      editor.update(() => $repairPasteSeqInvariants(), { discrete: true })
      unregisterPill = editor.registerNodeTransform(PasteBlockNode, $reseqDuplicatePill)
      unregisterText = editor.registerNodeTransform(TextNode, $reseqPillsCollidingWithText)
    })
    return () => {
      active = false
      unregisterText?.()
      unregisterPill?.()
    }
  }, [editor])
  return null
}
