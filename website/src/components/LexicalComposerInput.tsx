import { useCallback, useEffect, useMemo, useRef } from 'react'
import { LexicalComposer } from '@lexical/react/LexicalComposer'
import { ContentEditable } from '@lexical/react/LexicalContentEditable'
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin'
import { EditorRefPlugin } from '@lexical/react/LexicalEditorRefPlugin'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import { LexicalErrorBoundary } from '@lexical/react/LexicalErrorBoundary'
import { OnChangePlugin } from '@lexical/react/LexicalOnChangePlugin'
import { PlainTextPlugin } from '@lexical/react/LexicalPlainTextPlugin'
import {
  $createLineBreakNode,
  $createParagraphNode,
  $createRangeSelection,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isNodeSelection,
  $isRangeSelection,
  $isTextNode,
  $nodesOfType,
  $setSelection,
  CLEAR_HISTORY_COMMAND,
  COMMAND_PRIORITY_HIGH,
  COPY_COMMAND,
  CUT_COMMAND,
  type EditorState,
  type ElementNode,
  type LexicalEditor,
  type LexicalNode,
  type PointType,
  INSERT_LINE_BREAK_COMMAND,
  KEY_BACKSPACE_COMMAND,
  KEY_DELETE_COMMAND,
  KEY_ARROW_DOWN_COMMAND,
  KEY_ARROW_UP_COMMAND,
  KEY_ENTER_COMMAND,
  KEY_MODIFIER_COMMAND,
  PASTE_COMMAND,
} from 'lexical'
import { INPUT_TYPO } from './PasteHighlightLayer'
import type { ComposerControl, ComposerSelection } from './composerControl'
import {
  clipboardFiles,
  hasPlainClipboardText,
  stripTrailingBlankLines,
} from './composerPastePolicy'
import {
  $createPasteTokenNode,
  $isPasteTokenNode,
  PasteTokenNode,
} from './PasteTokenNode'
import {
  countLines,
  findTokenRanges,
  makePasteId,
  nextSeq,
  shouldCollapse,
  type PasteBlock,
} from '../utils/pasteTokens'
import type { SendMode } from '../pages/chat/ChatSettings'

const CONTROLLED_SYNC_TAG = 'kirocrew-controlled-composer-sync'

interface LexicalComposerInputProps {
  value: string
  blocks: PasteBlock[]
  onChange: (value: string) => void
  onBlocksChange?: (blocks: PasteBlock[]) => void
  onSend: () => void
  ariaLabel: string
  placeholder: string
  disabled?: boolean
  readOnly?: boolean
  sendOnEnter?: SendMode
  className?: string
  controlRef?: React.MutableRefObject<ComposerControl | null>
  editorRef?: React.RefCallback<LexicalEditor> | React.RefObject<LexicalEditor | null | undefined>
  onReady?: () => void
  onSelectionChange?: (selection: ComposerSelection) => void
  onUploadFiles?: (files: File[]) => void
  sentMessages?: string[]
}

function appendPlainText(text: string, append: (node: ReturnType<typeof $createTextNode> | ReturnType<typeof $createLineBreakNode>) => void) {
  const lines = text.split('\n')
  lines.forEach((line, index) => {
    if (line) append($createTextNode(line))
    if (index < lines.length - 1) append($createLineBreakNode())
  })
}

function $replaceComposerValue(value: string, blocks: PasteBlock[]): void {
  const root = $getRoot()
  root.clear()
  const paragraph = $createParagraphNode()
  root.append(paragraph)
  const ranges = findTokenRanges(value, blocks)
  let cursor = 0
  for (const range of ranges) {
    appendPlainText(value.slice(cursor, range.start), node => paragraph.append(node))
    paragraph.append($createPasteTokenNode(range.block))
    cursor = range.end
  }
  appendPlainText(value.slice(cursor), node => paragraph.append(node))
}

function sameBlocks(left: PasteBlock[], right: PasteBlock[]): boolean {
  return left.length === right.length && left.every((block, index) => {
    const other = right[index]
    return other !== undefined && block.id === other.id && block.seq === other.seq &&
      block.lines === other.lines && block.content === other.content
  })
}

function $composerSnapshot(): { value: string; blocks: PasteBlock[] } {
  return {
    value: $getRoot().getTextContent(),
    blocks: $nodesOfType(PasteTokenNode).map(node => node.getBlock()),
  }
}

function $nodeStartOffset(node: LexicalNode): number {
  let offset = 0
  let current: LexicalNode | null = node
  while (current) {
    let sibling = current.getPreviousSibling()
    while (sibling) {
      offset += sibling.getTextContentSize()
      sibling = sibling.getPreviousSibling()
    }
    current = current.getParent()
  }
  return offset
}

function $pointOffset(point: PointType): number {
  const node = point.getNode()
  if (point.type === 'text') return $nodeStartOffset(node) + point.offset
  if (!$isElementNode(node)) return $nodeStartOffset(node)
  return $nodeStartOffset(node) + node.getChildren().slice(0, point.offset)
    .reduce((total, child) => total + child.getTextContentSize(), 0)
}

function $canonicalSelection(): ComposerSelection | null {
  const selection = $getSelection()
  if ($isRangeSelection(selection)) {
    const anchor = $pointOffset(selection.anchor)
    const focus = $pointOffset(selection.focus)
    return { start: Math.min(anchor, focus), end: Math.max(anchor, focus) }
  }
  if ($isNodeSelection(selection)) {
    const nodes = selection.getNodes()
    if (!nodes.length) return null
    const starts = nodes.map($nodeStartOffset)
    const ends = nodes.map(node => $nodeStartOffset(node) + node.getTextContentSize())
    return { start: Math.min(...starts), end: Math.max(...ends) }
  }
  return null
}

function $setPointAtOffset(point: PointType, offset: number): void {
  const root = $getRoot()
  const bounded = Math.max(0, Math.min(offset, root.getTextContentSize()))
  const visit = (parent: ElementNode, localOffset: number): void => {
    let consumed = 0
    const children = parent.getChildren()
    for (let index = 0; index < children.length; index += 1) {
      const child = children[index]
      const size = child.getTextContentSize()
      const next = consumed + size
      if (localOffset <= next) {
        if ($isTextNode(child)) {
          point.set(child.getKey(), Math.max(0, localOffset - consumed), 'text')
          return
        }
        if ($isElementNode(child)) {
          visit(child, Math.max(0, localOffset - consumed))
          return
        }
        point.set(parent.getKey(), index + (localOffset > consumed ? 1 : 0), 'element')
        return
      }
      consumed = next
    }
    point.set(parent.getKey(), children.length, 'element')
  }
  visit(root, bounded)
}

function ComposerControlPlugin({
  controlRef,
  onReady,
  onSelectionChange,
}: {
  controlRef?: React.MutableRefObject<ComposerControl | null>
  onReady?: () => void
  onSelectionChange?: (selection: ComposerSelection) => void
}) {
  const [editor] = useLexicalComposerContext()

  useEffect(() => {
    if (!controlRef && !onSelectionChange) return
    const control: ComposerControl = {
      focus: () => {
        editor.getRootElement()?.focus()
        editor.focus()
      },
      getRootElement: () => editor.getRootElement(),
      getSelection: () => {
        let selection: ComposerSelection | null = null
        editor.getEditorState().read(() => { selection = $canonicalSelection() })
        return selection
      },
      setSelection: (start, end = start, options) => {
        editor.update(() => {
          const selection = $createRangeSelection()
          $setPointAtOffset(selection.anchor, start)
          $setPointAtOffset(selection.focus, end)
          $setSelection(selection)
        }, { discrete: true })
        if (options?.focus) editor.focus()
      },
    }
    if (controlRef) controlRef.current = control
    onReady?.()
    let previous = ''
    const unregister = editor.registerUpdateListener(({ editorState }) => {
      if (!onSelectionChange) return
      const selection = editorState.read(() => $canonicalSelection())
      if (!selection) return
      const key = `${selection.start}:${selection.end}`
      if (key === previous) return
      previous = key
      onSelectionChange(selection)
    })
    return () => {
      unregister()
      if (controlRef?.current === control) controlRef.current = null
    }
  }, [controlRef, editor, onReady, onSelectionChange])

  return null
}

function ControlledValuePlugin({
  value,
  blocks,
  lastEmittedRef,
}: {
  value: string
  blocks: PasteBlock[]
  lastEmittedRef: React.MutableRefObject<{ value: string; blocks: PasteBlock[] }>
}) {
  const [editor] = useLexicalComposerContext()

  useEffect(() => {
    const emitted = lastEmittedRef.current
    if (emitted.value === value && sameBlocks(emitted.blocks, blocks)) return
    let current = { value: '', blocks: [] as PasteBlock[] }
    editor.getEditorState().read(() => { current = $composerSnapshot() })
    if (current.value === value && sameBlocks(current.blocks, blocks)) return
    lastEmittedRef.current = { value, blocks }
    editor.update(() => $replaceComposerValue(value, blocks), {
      tag: CONTROLLED_SYNC_TAG,
      discrete: true,
    })
    editor.dispatchCommand(CLEAR_HISTORY_COMMAND, undefined)
  }, [blocks, editor, lastEmittedRef, value])

  return null
}

function EditableStatePlugin({ editable }: { editable: boolean }) {
  const [editor] = useLexicalComposerContext()
  useEffect(() => editor.setEditable(editable), [editable, editor])
  return null
}

function expandedSelectionText(): string | null {
  const selection = $getSelection()
  if (!selection) return null
  const nodes = $isRangeSelection(selection) ? selection.extract() : selection.getNodes()
  if (!nodes.some($isPasteTokenNode)) return null
  return nodes.map(node => $isPasteTokenNode(node) ? node.getBlock().content : node.getTextContent()).join('')
}

function InteractionPlugin({
  blocks,
  onBlocksChange,
  onChange,
  onSend,
  onUploadFiles,
  sentMessages,
  disabled,
  readOnly,
  sendOnEnter,
}: Pick<LexicalComposerInputProps, 'blocks' | 'onBlocksChange' | 'onChange' | 'onSend' | 'onUploadFiles' | 'sentMessages' | 'disabled' | 'readOnly' | 'sendOnEnter'>) {
  const [editor] = useLexicalComposerContext()
  const blocksRef = useRef(blocks)
  const rawPasteRef = useRef(false)
  const historyIndexRef = useRef(-1)
  const historyDraftRef = useRef('')
  blocksRef.current = blocks

  useEffect(() => {
    const unregisterModifier = editor.registerCommand(
      KEY_MODIFIER_COMMAND,
      event => {
        rawPasteRef.current = (event.metaKey || event.ctrlKey) && event.shiftKey &&
          !event.altKey && event.key.toLowerCase() === 'v'
        return false
      },
      COMMAND_PRIORITY_HIGH,
    )
    const unregisterPaste = editor.registerCommand(
      PASTE_COMMAND,
      (event) => {
        const forceRaw = rawPasteRef.current
        rawPasteRef.current = false
        if (disabled || readOnly || !('clipboardData' in event) || !event.clipboardData) return false
        const data = event.clipboardData
        const hasText = hasPlainClipboardText(data)
        const files = clipboardFiles(data)
        if (files.length && onUploadFiles && !hasText) {
          event.preventDefault()
          onUploadFiles(files)
          return true
        }
        if (!hasText) return false
        const pasted = data.getData('text/plain')
        const cleaned = forceRaw ? pasted : stripTrailingBlankLines(pasted)
        const selection = $getSelection()
        if (!$isRangeSelection(selection)) return false
        event.preventDefault()
        if (onBlocksChange && !forceRaw && shouldCollapse(cleaned)) {
          const block: PasteBlock = {
            id: makePasteId(),
            seq: nextSeq(blocksRef.current),
            lines: countLines(cleaned),
            content: cleaned,
          }
          selection.insertNodes([$createPasteTokenNode(block)])
          blocksRef.current = [...blocksRef.current, block]
          return true
        }
        selection.insertRawText(cleaned || pasted)
        return true
      },
      COMMAND_PRIORITY_HIGH,
    )

    const writeClipboard = (event: ClipboardEvent | KeyboardEvent | null, cut: boolean) => {
      if (!event || !('clipboardData' in event) || !event.clipboardData) return false
      const expanded = expandedSelectionText()
      if (expanded === null) return false
      event.clipboardData.setData('text/plain', expanded)
      event.preventDefault()
      if (cut) {
        const selection = $getSelection()
        if ($isRangeSelection(selection)) selection.removeText()
        else if ($isNodeSelection(selection)) selection.deleteNodes()
      }
      return true
    }

    const unregisterCopy = editor.registerCommand(
      COPY_COMMAND,
      event => writeClipboard(event, false),
      COMMAND_PRIORITY_HIGH,
    )
    const unregisterCut = editor.registerCommand(
      CUT_COMMAND,
      event => writeClipboard(event, true),
      COMMAND_PRIORITY_HIGH,
    )
    const deleteSelectedNode = (event: KeyboardEvent) => {
      const selection = $getSelection()
      if (!$isNodeSelection(selection) || !selection.getNodes().some($isPasteTokenNode)) return false
      event.preventDefault()
      selection.deleteNodes()
      return true
    }
    const unregisterBackspace = editor.registerCommand(KEY_BACKSPACE_COMMAND, deleteSelectedNode, COMMAND_PRIORITY_HIGH)
    const unregisterDelete = editor.registerCommand(KEY_DELETE_COMMAND, deleteSelectedNode, COMMAND_PRIORITY_HIGH)
    const unregisterEnter = editor.registerCommand(
      KEY_ENTER_COMMAND,
      (event) => {
        if (!event) return false
        if (event.isComposing || event.keyCode === 229) {
          event.preventDefault()
          return true
        }
        const commandKey = event.metaKey || event.ctrlKey
        if (sendOnEnter === 'enter-ctrl-newline' && commandKey) {
          event.preventDefault()
          editor.dispatchCommand(INSERT_LINE_BREAK_COMMAND, false)
          return true
        }
        const shouldSend = sendOnEnter === 'ctrl-enter'
          ? commandKey
          : !event.shiftKey
        if (!shouldSend) return false
        event.preventDefault()
        if (!disabled && !readOnly) onSend()
        return true
      },
      COMMAND_PRIORITY_HIGH,
    )

    const moveAfterRecall = (value: string, position: 'start' | 'end') => {
      requestAnimationFrame(() => {
        editor.update(() => {
          const offset = position === 'start' ? 0 : value.length
          const selection = $createRangeSelection()
          $setPointAtOffset(selection.anchor, offset)
          $setPointAtOffset(selection.focus, offset)
          $setSelection(selection)
        }, { discrete: true })
        editor.focus()
      })
    }
    const navigateHistory = (event: KeyboardEvent, direction: 'up' | 'down') => {
      if (!sentMessages?.length || event.isComposing || event.metaKey || event.ctrlKey ||
        event.altKey || event.shiftKey) return false
      const selection = $canonicalSelection()
      if (!selection || selection.start !== selection.end) return false
      const current = $getRoot().getTextContent()
      const last = sentMessages.length - 1
      if (direction === 'up') {
        if (current !== '' && selection.start !== 0) return false
        const index = historyIndexRef.current
        if (index === -1) {
          historyDraftRef.current = current
          historyIndexRef.current = last
        } else if (index > 0) {
          historyIndexRef.current = index - 1
        }
        const recalled = sentMessages[historyIndexRef.current]
        event.preventDefault()
        onChange(recalled)
        moveAfterRecall(recalled, 'start')
        return true
      }
      const index = historyIndexRef.current
      if (index === -1 || selection.end !== current.length) return false
      event.preventDefault()
      if (index < last) {
        historyIndexRef.current = index + 1
        const recalled = sentMessages[historyIndexRef.current]
        onChange(recalled)
        moveAfterRecall(recalled, 'end')
      } else {
        historyIndexRef.current = -1
        const draft = historyDraftRef.current
        historyDraftRef.current = ''
        onChange(draft)
        moveAfterRecall(draft, 'end')
      }
      return true
    }
    const unregisterArrowUp = editor.registerCommand(
      KEY_ARROW_UP_COMMAND,
      event => navigateHistory(event, 'up'),
      COMMAND_PRIORITY_HIGH,
    )
    const unregisterArrowDown = editor.registerCommand(
      KEY_ARROW_DOWN_COMMAND,
      event => navigateHistory(event, 'down'),
      COMMAND_PRIORITY_HIGH,
    )

    return () => {
      unregisterModifier()
      unregisterPaste()
      unregisterCopy()
      unregisterCut()
      unregisterBackspace()
      unregisterDelete()
      unregisterEnter()
      unregisterArrowUp()
      unregisterArrowDown()
    }
  }, [disabled, editor, onBlocksChange, onChange, onSend, onUploadFiles, readOnly, sendOnEnter, sentMessages])

  return null
}

export default function LexicalComposerInput({
  value,
  blocks,
  onChange,
  onBlocksChange,
  onSend,
  ariaLabel,
  placeholder,
  disabled = false,
  readOnly = false,
  sendOnEnter = 'enter',
  className = '',
  controlRef,
  editorRef,
  onReady,
  onSelectionChange,
  onUploadFiles,
  sentMessages,
}: LexicalComposerInputProps) {
  const initialValueRef = useRef({ value, blocks })
  const lastEmittedRef = useRef({ value, blocks })
  const initialConfig = useMemo(() => ({
    namespace: 'KiroCrewComposer',
    nodes: [PasteTokenNode],
    editable: !disabled && !readOnly,
    editorState: () => $replaceComposerValue(initialValueRef.current.value, initialValueRef.current.blocks),
    onError(error: Error, _editor: LexicalEditor) {
      throw error
    },
  }), [disabled, readOnly])

  const handleChange = useCallback((editorState: EditorState, _editor: LexicalEditor, tags: Set<string>) => {
    if (tags.has(CONTROLLED_SYNC_TAG)) return
    let next = { value: '', blocks: [] as PasteBlock[] }
    editorState.read(() => { next = $composerSnapshot() })
    lastEmittedRef.current = next
    if (next.value !== value) onChange(next.value)
    if (onBlocksChange && !sameBlocks(next.blocks, blocks)) onBlocksChange(next.blocks)
  }, [blocks, onBlocksChange, onChange, value])

  return (
    <LexicalComposer initialConfig={initialConfig}>
      <div className={`relative min-h-[44px] ${className}`}>
        <PlainTextPlugin
          contentEditable={
            <ContentEditable
              aria-label={ariaLabel}
              aria-multiline="true"
              data-composer-input=""
              data-lexical-composer=""
              className={`relative w-full min-h-[44px] max-h-[50vh] overflow-y-auto border-none bg-transparent text-text outline-none whitespace-pre-wrap break-words ${INPUT_TYPO}`}
            />
          }
          placeholder={
            <div className={`pointer-events-none absolute inset-0 overflow-hidden text-muted ${INPUT_TYPO}`}>
              {placeholder}
            </div>
          }
          ErrorBoundary={LexicalErrorBoundary}
        />
        <HistoryPlugin />
        <ComposerControlPlugin
          controlRef={controlRef}
          onReady={onReady}
          onSelectionChange={onSelectionChange}
        />
        {editorRef && <EditorRefPlugin editorRef={editorRef} />}
        <OnChangePlugin onChange={handleChange} ignoreSelectionChange />
        <ControlledValuePlugin value={value} blocks={blocks} lastEmittedRef={lastEmittedRef} />
        <EditableStatePlugin editable={!disabled && !readOnly} />
        <InteractionPlugin
          blocks={blocks}
          onBlocksChange={onBlocksChange}
          onChange={onChange}
          onSend={onSend}
          onUploadFiles={onUploadFiles}
          sentMessages={sentMessages}
          disabled={disabled}
          readOnly={readOnly}
          sendOnEnter={sendOnEnter}
        />
      </div>
    </LexicalComposer>
  )
}
