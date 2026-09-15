import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  $getNodeByKey,
  COMMAND_PRIORITY_HIGH,
  DRAGSTART_COMMAND,
  HISTORIC_TAG,
  SKIP_DOM_SELECTION_TAG,
  type NodeKey,
  type UpdateTag,
} from 'lexical'
import type { PasteBlock } from '../../utils/pasteTokens'
import { countLines } from '../../utils/pasteTokens'
import { PasteBlocksContext, type PasteBlocksContextValue } from '../PasteBlocksContext'
import PastePreviewEditor from '../PastePreviewEditor'
import { $isPasteBlockNode, PasteBlockNode } from '../nodes/PasteBlockNode'
import PillDragPlugin from './PillDragPlugin'
import PillKeyboardPlugin from './PillKeyboardPlugin'
import PasteSeqInvariantPlugin from './PasteSeqInvariantPlugin'

/**
 * Everything the inline paste pills need beyond the node itself, mounted once
 * inside the composer:
 *
 * - the `PasteBlocksContext` the decorated chips read (open the preview),
 * - the click-to-edit `PastePreviewEditor` popover,
 * - `PillDragPlugin` (drag a pill to reorder; a live insertion caret marks the
 *   drop point) and `PillKeyboardPlugin` (atomic Backspace/Delete, arrows step
 *   over a node-selected pill, typing never replaces one),
 * - `PasteSeqInvariantPlugin` (no two pills share a seq, so a marker always
 *   names exactly one block).
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
 *
 * The preview WRITES THROUGH: every keystroke in its textarea lands in the pill
 * node at once (and so in the host's block list and the persisted draft), and
 * Cancel restores the content the panel opened on. The panel therefore never
 * holds text that exists nowhere else — a browser reload, Back, or any other
 * teardown that unmounts it mid-edit finds the draft already carrying the edit,
 * where a panel that only committed on Save would have taken the text with it.
 */

/** The block a pill node stands for, or undefined once that node is gone. */
function $blockOf(key: NodeKey): PasteBlock | undefined {
  const node = $getNodeByKey(key)
  return $isPasteBlockNode(node) ? node.getBlock() : undefined
}

/**
 * Tags for the interim writes (each keystroke, and the revert on Cancel).
 * `historic` keeps them OUT of the undo history — Save records the edit as one
 * entry, exactly as a commit-on-Save did, instead of one entry per keystroke;
 * a cancelled edit records nothing. `skip-dom-selection` keeps Lexical from
 * touching the document selection while the panel's textarea owns it.
 */
const LIVE_EDIT_TAGS: UpdateTag[] = [HISTORIC_TAG, SKIP_DOM_SELECTION_TAG]

function sameRect(a: DOMRect, b: DOMRect): boolean {
  return a.left === b.left && a.top === b.top && a.width === b.width && a.height === b.height
}

interface Preview {
  key: NodeKey
  /** The chip element the panel is anchored to (re-measured while open). */
  anchor: HTMLElement
  rect: DOMRect
  /** The block as it was when the panel opened — what Cancel restores. */
  original: PasteBlock
}

export default function PillsPlugin({ children }: { children: ReactNode }) {
  const [editor] = useLexicalComposerContext()
  // The preview is bound to the pill NODE (its Lexical key), not to a seq or a
  // block id: a value holding the same marker twice rehydrates as two nodes
  // that share both, and a session switch swaps in another session's `Paste
  // #1` under the open popover. Only the key names exactly the pill that was
  // clicked, so a write can only ever land in that one.
  const [preview, setPreview] = useState<Preview | null>(null)
  const previewKey = preview?.key
  const previewAnchor = preview?.anchor

  // PlainTextPlugin cancels EVERY dragstart while a selection exists. Claim the
  // command for pill sources (without preventDefault) so the browser starts the
  // drag and PillDragPlugin takes over.
  useEffect(() => editor.registerCommand(
    DRAGSTART_COMMAND,
    event => !!(event.target as HTMLElement | null)?.closest?.('.pill-host'),
    COMMAND_PRIORITY_HIGH,
  ), [editor])

  // Drop the preview when its node leaves the tree (the ✕, a Backspace, the
  // host replacing the whole value on a session switch) rather than let it
  // rebind to whichever pill now carries that seq. No caret hand-back on
  // destroy: the pill it belonged to is gone. Data changes are NOT followed:
  // while the panel is open it is the node's only writer (its keys never reach
  // the editor, and focus is trapped inside it), so an `updated` mutation is
  // the panel's own write-through echoing back.
  useEffect(() => {
    if (previewKey === undefined) return
    return editor.registerMutationListener(PasteBlockNode, mutations => {
      if (mutations.get(previewKey) !== 'destroyed') return
      setPreview(current => (current && current.key === previewKey ? null : current))
    }, { skipInitialization: true })
  }, [editor, previewKey])

  // Keep the panel on its pill while open. The anchor rect is a snapshot, and
  // the pill moves under it: a window resize re-wraps the composer, scrolling
  // the composer's own overflow (or any scroller around it) slides the line,
  // and the write-through above re-labels the chip, which can change its width
  // and push it onto another line. Re-measure on each of those and hand the new
  // rect down; the editor re-derives its side and clamp from it.
  useEffect(() => {
    if (previewKey === undefined || !previewAnchor) return
    const remeasure = () => {
      // A re-created decorator leaves the captured element detached; the
      // node's wrapper is the fallback, and no element at all means the pill is
      // not on screen to anchor to.
      const el = previewAnchor.isConnected ? previewAnchor : editor.getElementByKey(previewKey)
      if (!el) { setPreview(current => (current && current.key === previewKey ? null : current)); return }
      const rect = el.getBoundingClientRect()
      setPreview(current => {
        if (!current || current.key !== previewKey || sameRect(current.rect, rect)) return current
        return { ...current, rect }
      })
    }
    window.addEventListener('resize', remeasure)
    // Capture phase: `scroll` does not bubble, and the scroller that moves the
    // pill can be any ancestor (the composer's overflow, the page).
    document.addEventListener('scroll', remeasure, true)
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(remeasure) : null
    ro?.observe(previewAnchor)
    return () => {
      window.removeEventListener('resize', remeasure)
      document.removeEventListener('scroll', remeasure, true)
      ro?.disconnect()
    }
  }, [editor, previewKey, previewAnchor])

  const openPreview = useCallback((key: NodeKey, anchor: HTMLElement) => {
    const block = editor.getEditorState().read(() => $blockOf(key))
    if (block) setPreview({ key, anchor, rect: anchor.getBoundingClientRect(), original: block })
  }, [editor])

  // Closing the preview hands the caret back: clicking a pill leaves Lexical on
  // a NodeSelection (no caret, pill ringed) and the popover's textarea took
  // focus. Park a RangeSelection right AFTER the pill and refocus the editor.
  const closePreview = useCallback(() => {
    const current = preview
    setPreview(null)
    editor.update(() => {
      if (!current) return
      const node = $getNodeByKey(current.key)
      if ($isPasteBlockNode(node)) node.selectNext(0, 0)
    }, { discrete: true })
    editor.getRootElement()?.focus({ preventScroll: true })
    editor.focus()
  }, [editor, preview])

  const updateBlock = useCallback((key: NodeKey, content: string, tags?: UpdateTag[]) => {
    editor.update(() => {
      const node = $getNodeByKey(key)
      if ($isPasteBlockNode(node)) node.setData(countLines(content), content)
    }, tags ? { tag: tags } : undefined)
  }, [editor])

  // Cancel: put back what the panel opened on, if the write-through moved it.
  const revertPreview = useCallback(() => {
    if (!preview) return
    const current = editor.getEditorState().read(() => $blockOf(preview.key))
    if (current && current.content !== preview.original.content) {
      updateBlock(preview.key, preview.original.content, LIVE_EDIT_TAGS)
    }
  }, [editor, preview, updateBlock])

  const contextValue = useMemo<PasteBlocksContextValue>(() => ({ openPreview }), [openPreview])

  return (
    <PasteBlocksContext.Provider value={contextValue}>
      {children}
      <PasteSeqInvariantPlugin />
      <PillDragPlugin />
      <PillKeyboardPlugin />
      <PastePreviewEditor
        open={!!preview}
        anchorRect={preview?.rect ?? null}
        content={preview?.original.content ?? ''}
        lines={preview?.original.lines ?? 0}
        sessionKey={preview?.key}
        onChange={content => { if (preview) updateBlock(preview.key, content, LIVE_EDIT_TAGS) }}
        onSave={content => {
          // The untagged write is the one history entry for the whole edit.
          if (preview) updateBlock(preview.key, content)
          closePreview()
        }}
        onClose={() => { revertPreview(); closePreview() }}
      />
    </PasteBlocksContext.Provider>
  )
}
