import { useCallback } from 'react'
import {
  $getNodeByKey,
  DecoratorNode,
  type EditorConfig,
  type LexicalEditor,
  type LexicalNode,
  type NodeKey,
  type SerializedLexicalNode,
} from 'lexical'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { useLexicalNodeSelection } from '@lexical/react/useLexicalNodeSelection'

import PasteBlockChip, { pasteFirstLine, pasteSnippet } from '../PasteBlockChip'
import { usePasteBlocks } from '../PasteBlocksContext'
import { formatToken, makePasteId, type PasteBlock } from '../../utils/pasteTokens'

/**
 * Atomic inline decorator for one paste token (`[ Paste #N · M lines ]`).
 *
 * The node CARRIES the block data itself — `__seq`, `__lines`, `__content` — so
 * an undo/redo that re-inserts the node is self-contained even if the outer
 * `pasteBlocks` array has already pruned the block; the host derives its block
 * list from the tree, so a re-inserted node simply reappears in it.
 *
 * `getTextContent()` returns the literal token so serialization (and any string
 * read) stays byte-identical to the classic textarea value.
 */
export interface PasteBlockPayload {
  /** Sidecar id (React key / block identity). Optional for callers that only
   *  know the token; a fresh id is minted so the node always carries a whole
   *  `PasteBlock`. */
  id?: string
  seq: number
  lines: number
  content: string
}

export type SerializedPasteBlockNode = SerializedLexicalNode & {
  id: string
  seq: number
  lines: number
  content: string
}

/**
 * The decorated chip. Reads app behaviour from `PasteBlocksContext` and its
 * selected state from `useLexicalNodeSelection`. Passes the node's OWN seq/lines
 * (not the context block) so it renders correctly even mid-undo before the
 * context array has caught up.
 */
function PasteBlockChipInEditor({
  nodeKey,
  seq,
  lines,
  content,
}: {
  nodeKey: NodeKey
  seq: number
  lines: number
  content: string
}) {
  const [editor] = useLexicalComposerContext()
  const { openPreview } = usePasteBlocks()
  const [isSelected, setSelected] = useLexicalNodeSelection(nodeKey)

  const onOpen = useCallback(
    (el: HTMLElement) => {
      // Clicking a pill selects it (ring) as well as opening its preview, so a
      // following copy/cut/Backspace acts on the pill; closing the preview
      // parks the caret right after it again. The preview is bound to THIS
      // node (by key): two pills may legitimately share a seq.
      setSelected(true)
      openPreview(nodeKey, el)
    },
    [nodeKey, openPreview, setSelected],
  )

  const onRemove = useCallback(() => {
    // Remove exactly this node from the tree — never "the pill with this seq",
    // which would also reap a twin carrying the same marker. The host's
    // snapshot drops the block once no node stands for it.
    editor.update(() => {
      $getNodeByKey(nodeKey)?.remove()
    })
  }, [editor, nodeKey])

  // Tab-focused chip → ←/→ hand the caret back to the editor on either side.
  const onEscape = useCallback(
    (side: 'before' | 'after') => {
      // DOM focus first (explicitly — a DOM selection change alone does not move
      // focus everywhere), then place the Lexical caret on the requested side.
      editor.getRootElement()?.focus({ preventScroll: true })
      editor.update(() => {
        const node = $getNodeByKey(nodeKey)
        if (!node) return
        if (side === 'before') node.selectPrevious()
        else node.selectNext(0, 0)
      }, { discrete: true })
    },
    [editor, nodeKey],
  )

  return (
    <PasteBlockChip
      seq={seq}
      lines={lines}
      snippet={pasteSnippet(content)}
      firstLine={pasteFirstLine(content)}
      state={isSelected ? 'selected' : 'idle'}
      draggable
      onOpen={onOpen}
      onRemove={onRemove}
      onEscape={onEscape}
    />
  )
}

export class PasteBlockNode extends DecoratorNode<JSX.Element> {
  __id: string
  __seq: number
  __lines: number
  __content: string

  static getType(): string {
    return 'paste-block'
  }

  static clone(node: PasteBlockNode): PasteBlockNode {
    return new PasteBlockNode(
      { id: node.__id, seq: node.__seq, lines: node.__lines, content: node.__content },
      node.__key,
    )
  }

  constructor(payload: PasteBlockPayload, key?: NodeKey) {
    super(key)
    this.__id = payload.id ?? makePasteId()
    this.__seq = payload.seq
    this.__lines = payload.lines
    this.__content = payload.content
  }

  static importJSON(serialized: SerializedPasteBlockNode): PasteBlockNode {
    return $createPasteBlockNode({
      id: serialized.id,
      seq: serialized.seq,
      lines: serialized.lines,
      content: serialized.content,
    })
  }

  exportJSON(): SerializedPasteBlockNode {
    return {
      type: 'paste-block',
      version: 1,
      id: this.__id,
      seq: this.__seq,
      lines: this.__lines,
      content: this.__content,
    }
  }

  createDOM(): HTMLElement {
    const span = document.createElement('span')
    span.className = 'pill-host'
    // Breathing room from the surrounding text on both sides.
    span.style.marginInline = '4px'
    return span
  }

  updateDOM(): false {
    return false
  }

  isInline(): boolean {
    return true
  }

  /**
   * Arrow keys step OVER the pill instead of parking on it as a node selection.
   * A keyboard-selectable decorator has a fatal default: typing while it is
   * selected REPLACES it, i.e. one stray keystroke silently discards the whole
   * paste. Keyboard access to the pill itself goes through the chip's own
   * focus (Tab → Enter opens, Backspace removes; see PasteBlockChip).
   */
  isKeyboardSelectable(): boolean {
    return false
  }

  canInsertTextBefore(): boolean {
    return true
  }

  canInsertTextAfter(): boolean {
    return true
  }

  getSeq(): number {
    return this.getLatest().__seq
  }

  /** The whole sidecar block this pill stands for (same shape ChatPage keeps). */
  getBlock(): PasteBlock {
    const self = this.getLatest()
    return { id: self.__id, seq: self.__seq, lines: self.__lines, content: self.__content }
  }

  getLines(): number {
    return this.getLatest().__lines
  }

  getContent(): string {
    return this.getLatest().__content
  }

  /** Mutate lines + content in place (used when the preview saves an edit). */
  setData(lines: number, content: string): void {
    const self = this.getWritable()
    self.__lines = lines
    self.__content = content
  }

  /**
   * Give this pill a new identity (fresh id + seq). Used by
   * `PasteSeqInvariantPlugin` alone, to split two pills that share a seq: the
   * serialized marker changes with the seq, so the host's value names this
   * block unambiguously again.
   */
  setIdentity(id: string, seq: number): void {
    const self = this.getWritable()
    self.__id = id
    self.__seq = seq
  }

  /** The literal token text, so any string read matches the classic value. */
  getTextContent(): string {
    const self = this.getLatest()
    return formatToken({ id: '', seq: self.__seq, lines: self.__lines, content: self.__content })
  }

  decorate(_editor: LexicalEditor, _config: EditorConfig): JSX.Element {
    return (
      <PasteBlockChipInEditor
        nodeKey={this.getKey()}
        seq={this.__seq}
        lines={this.__lines}
        content={this.__content}
      />
    )
  }
}

export function $createPasteBlockNode(payload: PasteBlockPayload): PasteBlockNode {
  return new PasteBlockNode(payload)
}

export function $isPasteBlockNode(
  node: LexicalNode | null | undefined,
): node is PasteBlockNode {
  return node instanceof PasteBlockNode
}
