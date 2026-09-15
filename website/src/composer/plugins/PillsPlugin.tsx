import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { $nodesOfType, COMMAND_PRIORITY_HIGH, DRAGSTART_COMMAND } from 'lexical'
import type { PasteBlock } from '../../utils/pasteTokens'
import { countLines } from '../../utils/pasteTokens'
import { PasteBlocksContext, type PasteBlocksContextValue } from '../PasteBlocksContext'
import PastePreviewEditor from '../PastePreviewEditor'
import { PasteBlockNode } from '../nodes/PasteBlockNode'
import PillDragPlugin from './PillDragPlugin'
import PillKeyboardPlugin from './PillKeyboardPlugin'

/**
 * Everything the inline paste pills need beyond the node itself, mounted once
 * inside the composer:
 *
 * - the `PasteBlocksContext` the decorated chips read (open the preview,
 *   remove),
 * - the click-to-edit `PastePreviewEditor` popover,
 * - `PillDragPlugin` (drag a pill to reorder; the text opens a live gap) and
 *   `PillKeyboardPlugin` (atomic Backspace/Delete, arrows step over a
 *   node-selected pill, typing never replaces one).
 *
 * MUST wrap the plugin that renders the decorators (`PlainTextPlugin` /
 * `RichTextPlugin`): Lexical mounts decorator components from that plugin's
 * React subtree, so a context provider that is merely a sibling never reaches
 * the chips.
 *
 * The block list itself is derived from the tree by the host's snapshot
 * (`$nodesOfType(PasteBlockNode)`), so removing a pill is just removing its
 * node and saving an edit is `node.setData(...)`; the host's OnChange reports
 * the new value + blocks.
 */
export default function PillsPlugin({ blocks, children }: { blocks: PasteBlock[]; children: ReactNode }) {
  const [editor] = useLexicalComposerContext()
  // The preview is bound to the block's stable `id`, not only its `seq`: seq is
  // per-session, so when the host swaps the whole value (a session switch
  // while the popover is open) another session's `Paste #1` would otherwise
  // slide under the open editor and take the Save.
  const [preview, setPreview] = useState<{ id: string; seq: number; rect: DOMRect } | null>(null)
  const bySeq = useMemo(() => new Map(blocks.map(b => [b.seq, b])), [blocks])

  // PlainTextPlugin cancels EVERY dragstart while a selection exists. Claim the
  // command for pill sources (without preventDefault) so the browser starts the
  // drag and PillDragPlugin takes over.
  useEffect(() => editor.registerCommand(
    DRAGSTART_COMMAND,
    event => !!(event.target as HTMLElement | null)?.closest?.('.pill-host'),
    COMMAND_PRIORITY_HIGH,
  ), [editor])

  const $findPill = (seq: number, id?: string): PasteBlockNode | undefined =>
    $nodesOfType(PasteBlockNode).find(n => n.getSeq() === seq && (id === undefined || n.getBlock().id === id))

  const openPreview = useCallback((seq: number, anchor: HTMLElement) => {
    const block = bySeq.get(seq)
    if (block) setPreview({ id: block.id, seq, rect: anchor.getBoundingClientRect() })
  }, [bySeq])

  // Closing the preview hands the caret back: clicking a pill leaves Lexical on
  // a NodeSelection (no caret, pill ringed) and the popover's textarea took
  // focus. Park a RangeSelection right AFTER the pill and refocus the editor.
  const closePreview = useCallback(() => {
    const current = preview
    setPreview(null)
    editor.update(() => {
      if (!current) return
      $findPill(current.seq, current.id)?.selectNext(0, 0)
    }, { discrete: true })
    editor.getRootElement()?.focus({ preventScroll: true })
    editor.focus()
  }, [editor, preview])

  const removeBlock = useCallback((seq: number) => {
    editor.update(() => { $findPill(seq)?.remove() })
  }, [editor])

  const updateBlock = useCallback((seq: number, content: string, id?: string) => {
    editor.update(() => { $findPill(seq, id)?.setData(countLines(content), content) })
  }, [editor])

  const contextValue = useMemo<PasteBlocksContextValue>(() => ({ openPreview, removeBlock }), [openPreview, removeBlock])

  const candidate = preview ? bySeq.get(preview.seq) : undefined
  const previewBlock = candidate && preview && candidate.id === preview.id ? candidate : undefined

  // The block the popover was opened for is no longer in the tree — the pill
  // was removed, or the host replaced the value (session switch). Drop the
  // preview rather than let it rebind to whichever block now carries that seq;
  // no caret hand-back, since the pill it belonged to is gone.
  useEffect(() => {
    if (preview && !previewBlock) setPreview(null)
  }, [preview, previewBlock])

  return (
    <PasteBlocksContext.Provider value={contextValue}>
      {children}
      <PillDragPlugin />
      <PillKeyboardPlugin />
      <PastePreviewEditor
        open={!!preview && !!previewBlock}
        anchorRect={preview?.rect ?? null}
        content={previewBlock?.content ?? ''}
        lines={previewBlock?.lines ?? 0}
        onSave={content => {
          if (preview && previewBlock) updateBlock(preview.seq, content, preview.id)
          closePreview()
        }}
        onClose={closePreview}
      />
    </PasteBlocksContext.Provider>
  )
}
