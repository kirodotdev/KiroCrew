import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { useLexicalNodeSelection } from '@lexical/react/useLexicalNodeSelection'
import {
  $applyNodeReplacement,
  $createNodeSelection,
  $getNodeByKey,
  $getSelection,
  $isNodeSelection,
  $setSelection,
  DecoratorNode,
  type EditorConfig,
  type LexicalEditor,
  type NodeKey,
  type SerializedLexicalNode,
} from 'lexical'
import type { JSX } from 'react'
import PastePreviewTooltip from './PastePreviewTooltip'
import { PREVIEW_MAX_HEIGHT, PREVIEW_OPEN_DELAY_MS } from './pastePreviewConstants'
import { formatToken, type PasteBlock } from '../utils/pasteTokens'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import Clickable from './Clickable'

export type SerializedPasteTokenNode = SerializedLexicalNode & {
  block: PasteBlock
  type: 'paste-token'
  version: 1
}

function PasteTokenChip({ block, nodeKey }: { block: PasteBlock; nodeKey: NodeKey }) {
  useLanguageGeneration()
  const serializedLabel = i18nT('components.pastedChip.paste_lines', { seq: block.seq, count: block.lines })
  const displayLabel = serializedLabel.replace(/^\[\s*/, '').replace(/\s*\]$/, '')
  const [editor] = useLexicalComposerContext()
  const [selected] = useLexicalNodeSelection(nodeKey)
  const [anchor, setAnchor] = useState<{ left: number; top: number; below: boolean } | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const panelId = useId()

  const closePreview = () => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = null
    setAnchor(null)
  }

  const openPreview = () => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = null
    const nodeElement = editor.getElementByKey(nodeKey)
    if (!nodeElement) return
    const rect = nodeElement.getBoundingClientRect()
    const below = window.innerHeight - rect.bottom >= PREVIEW_MAX_HEIGHT + 24
    setAnchor({ left: rect.left, top: below ? rect.bottom + 4 : rect.top - 4, below })
  }

  const schedulePreview = () => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(openPreview, PREVIEW_OPEN_DELAY_MS)
  }

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current)
  }, [])

  useEffect(() => {
    if (!anchor) return
    const close = () => setAnchor(null)
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [anchor])

  const previewOpen = anchor !== null
  const actionLabel = i18nT(previewOpen ? 'components.pastedChip.collapse_paste' : 'components.pastedChip.expand_paste')
  return (
    <>
      <Clickable
        contentEditable={false}
        data-testid={`paste-token-${block.seq}`}
        data-paste-seq={block.seq}
        aria-label={`${actionLabel}: ${displayLabel}`}
        title={actionLabel}
        aria-expanded={previewOpen}
        aria-describedby={previewOpen ? panelId : undefined}
        onKeyDown={(event) => {
          if (event.key !== 'Backspace' && event.key !== 'Delete') return
          event.preventDefault()
          closePreview()
          editor.update(() => { $getNodeByKey(nodeKey)?.remove() })
        }}
        onClick={(event) => {
          event?.preventDefault()
          const extend = event?.shiftKey ?? false
          editor.update(() => {
            const current = $getSelection()
            const selection = extend && $isNodeSelection(current) ? current : $createNodeSelection()
            if (!selection.has(nodeKey)) selection.add(nodeKey)
            $setSelection(selection)
          })
          if (previewOpen) closePreview()
          else openPreview()
        }}
        onMouseEnter={schedulePreview}
        onMouseLeave={closePreview}
        onFocus={schedulePreview}
        onBlur={closePreview}
        className={`inline-flex max-w-full items-center rounded-md border px-1.5 py-0.5 align-baseline text-[12px] font-body leading-none cursor-default select-none transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent ${selected ? 'border-accent bg-accent-subtle text-accent' : 'border-border bg-accent-subtle text-text'}`}
      >
        {displayLabel}
      </Clickable>
      {createPortal(
        <PastePreviewTooltip
          open={previewOpen}
          panelId={panelId}
          anchor={anchor}
          content={block.content}
          seq={block.seq}
          testIdPrefix="lexical-paste-preview"
        />,
        document.body,
      )}
    </>
  )
}

export class PasteTokenNode extends DecoratorNode<JSX.Element> {
  __block: PasteBlock

  static getType(): string {
    return 'paste-token'
  }

  static clone(node: PasteTokenNode): PasteTokenNode {
    return new PasteTokenNode(node.__block, node.__key)
  }

  static importJSON(serializedNode: SerializedPasteTokenNode): PasteTokenNode {
    return $createPasteTokenNode(serializedNode.block)
  }

  constructor(block: PasteBlock, key?: NodeKey) {
    super(key)
    this.__block = block
  }

  exportJSON(): SerializedPasteTokenNode {
    return {
      ...super.exportJSON(),
      block: this.__block,
      type: 'paste-token',
      version: 1,
    }
  }

  createDOM(_config: EditorConfig): HTMLElement {
    const element = document.createElement('span')
    element.contentEditable = 'false'
    element.dataset.pasteTokenId = this.__block.id
    element.style.display = 'inline'
    return element
  }

  updateDOM(): false {
    return false
  }

  decorate(_editor: LexicalEditor, _config: EditorConfig): JSX.Element {
    return <PasteTokenChip block={this.__block} nodeKey={this.__key} />
  }

  getTextContent(): string {
    return formatToken(this.__block)
  }

  getBlock(): PasteBlock {
    return this.getLatest().__block
  }

  isInline(): true {
    return true
  }
}

export function $createPasteTokenNode(block: PasteBlock): PasteTokenNode {
  return $applyNodeReplacement(new PasteTokenNode(block))
}

export function $isPasteTokenNode(node: unknown): node is PasteTokenNode {
  return node instanceof PasteTokenNode
}
