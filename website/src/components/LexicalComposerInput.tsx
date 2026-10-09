import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
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
  DELETE_CHARACTER_COMMAND,
  type EditorState,
  type ElementNode,
  type LexicalEditor,
  type LexicalNode,
  type PointType,
  INSERT_LINE_BREAK_COMMAND,
  INSERT_PARAGRAPH_COMMAND,
  KEY_BACKSPACE_COMMAND,
  KEY_DELETE_COMMAND,
  KEY_ARROW_DOWN_COMMAND,
  KEY_ARROW_UP_COMMAND,
  KEY_ENTER_COMMAND,
  KEY_MODIFIER_COMMAND,
  PASTE_COMMAND,
  REDO_COMMAND,
  UNDO_COMMAND,
  COMMAND_PRIORITY_CRITICAL,
} from 'lexical'
import { INPUT_TYPO } from './PasteHighlightLayer'
import { MacLineEdgePlugin } from './composerLineEdge'
import { createImeLatch } from '../hooks/useImeGuard'
import { IS_MAC } from '../hooks/useKeyboardShortcuts'
import type { ComposerControl, ComposerSelection } from './composerControl'
import { livePromptHistoryCursor, stepPromptHistory, type PromptHistoryCursor, type PromptHistoryItem } from './composerPromptHistory'
import {
  isRawPasteChord,
  clipboardFiles,
  hasPlainClipboardText,
  stripTrailingBlankLines,
} from './composerPastePolicy'
import { listLineBreakEdit, listMarkerBackspaceEdit, type ListLineBreakEdit } from './composerListContinuation'
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
import InlineMarkdownPlugin, { INLINE_MARKDOWN_THEME } from './InlineMarkdownPlugin'

const CONTROLLED_SYNC_TAG = 'kirocrew-controlled-composer-sync'

interface LexicalComposerInputProps {
  value: string
  blocks: PasteBlock[]
  onChange: (value: string) => void
  onBlocksChange?: (blocks: PasteBlock[]) => void
  /** Leave a long paste as full editable text instead of collapsing it into a
   *  paste-token chip. Defaults false; Cmd/Ctrl+Shift+V is the per-paste
   *  equivalent when it is off. */
  showFullPastes?: boolean
  /** Fire a send. `alternate` is true for Cmd/Ctrl+Enter in the `enter` send
   *  mode, which the host reads as "perform the OTHER busy action" (steer vs
   *  queue) for this one send, exactly as the textarea's key handler does. */
  onSend: (alternate?: boolean) => void
  /** Cmd/Ctrl+Shift+Enter. When set, the chord is claimed and this runs
   *  instead of a send; when unset the chord falls through to the send rules
   *  (a host with no Optimize affordance). */
  onOptimizeChord?: () => void
  ariaLabel: string
  placeholder: string
  disabled?: boolean
  readOnly?: boolean
  sendOnEnter?: SendMode
  /** Draw the browser's red spellcheck underlines under the input. Default true
   *  (Chromium's own default); the Settings composer toggle drives it off. */
  spellCheck?: boolean
  /** Style bold, italic, strikethrough and inline code as the user types.
   *  Display only: the value stays the markdown they typed. Default false. */
  inlineMarkdown?: boolean
  className?: string
  controlRef?: React.MutableRefObject<ComposerControl | null>
  editorRef?: React.RefCallback<LexicalEditor> | React.RefObject<LexicalEditor | null | undefined>
  onReady?: () => void
  onSelectionChange?: (selection: ComposerSelection) => void
  /** Take over undo/redo. The composer is controlled, so every value the
   *  host pushes in clears Lexical's own history; a host that keeps its own
   *  snapshot history answers here and returns true when it handled the step.
   *  Without it Lexical's HistoryPlugin handles undo as usual. */
  onHistoryStep?: (direction: 'undo' | 'redo') => boolean
  /** Close the host's coalescing typing burst, so a structural edit the
   *  composer makes (a list-marker Backspace) gets its own undo step. */
  onEndUndoBurst?: () => void
  onUploadFiles?: (files: File[]) => void
  sentMessages?: PromptHistoryItem[]
  /** Owner of `sentMessages` (the slot); a change ends prompt-history browsing. */
  historyScope?: string | null
  /** ⌘↑ (macOS) / Ctrl+↑ elsewhere — edit the last user message.
   *  Fired by the editor only from an EMPTY composer, so it cannot shadow
   *  ordinary caret movement or the ↑/↓ history recall below. */
  onEditLastRequest?: () => void
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

// Continue or end the markdown list item under a collapsed caret; false leaves
// the ordinary line break to PlainTextPlugin. One editor update, one undo step.
function $applyListLineBreak(): boolean {
  const selection = $getSelection()
  if (!$isRangeSelection(selection) || !selection.isCollapsed()) return false
  const chips = $nodesOfType(PasteTokenNode).map(node => {
    const start = $nodeStartOffset(node)
    return { start, end: start + node.getTextContentSize() }
  })
  const edit = listLineBreakEdit($getRoot().getTextContent(), $pointOffset(selection.anchor), chips)
  if (!edit) return false
  $applyRenumber(edit)
  const range = $createRangeSelection()
  $setPointAtOffset(range.anchor, edit.start)
  $setPointAtOffset(range.focus, edit.end)
  $setSelection(range)
  if (edit.insert) range.insertRawText(edit.insert)
  else range.removeText()
  return true
}

// The number rewrites below the edited item, last to first: each sits after
// the edit, so writing them never moves an offset still to be used.
function $applyRenumber(edit: ListLineBreakEdit): void {
  for (const write of [...(edit.renumber ?? [])].reverse()) {
    const digits = $createRangeSelection()
    $setPointAtOffset(digits.anchor, write.start)
    $setPointAtOffset(digits.focus, write.end)
    $setSelection(digits)
    digits.insertRawText(write.insert)
  }
}

// Remove the list marker in front of a collapsed caret in one Backspace
// (listMarkerBackspaceEdit); false leaves the one-character delete to
// PlainTextPlugin. One editor update, one undo step: `endUndoBurst` runs
// before the edit so the host's typing snapshot is kept, not overwritten.
function $applyListMarkerBackspace(endUndoBurst?: () => void): boolean {
  const selection = $getSelection()
  if (!$isRangeSelection(selection) || !selection.isCollapsed()) return false
  const chips = $nodesOfType(PasteTokenNode).map(node => {
    const start = $nodeStartOffset(node)
    return { start, end: start + node.getTextContentSize() }
  })
  const edit = listMarkerBackspaceEdit($getRoot().getTextContent(), $pointOffset(selection.anchor), chips)
  if (!edit) return false
  endUndoBurst?.()
  $applyRenumber(edit)
  const range = $createRangeSelection()
  $setPointAtOffset(range.anchor, edit.start)
  $setPointAtOffset(range.focus, edit.end)
  $setSelection(range)
  range.removeText()
  return true
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

/** Routes UNDO/REDO to the host's snapshot history ahead of HistoryPlugin.
 *  The step is handled either way: when the host has nothing to step to, the
 *  command is still consumed, because Lexical's own history was cleared by the
 *  last controlled update and would otherwise undo into a stale state. */
function HostHistoryPlugin({ onHistoryStep }: { onHistoryStep: (direction: 'undo' | 'redo') => boolean }) {
  const [editor] = useLexicalComposerContext()
  const stepRef = useRef(onHistoryStep)
  stepRef.current = onHistoryStep
  // Layout effect, like Lexical's own plugins: see InteractionPlugin.
  useLayoutEffect(() => {
    const offUndo = editor.registerCommand(UNDO_COMMAND, () => { stepRef.current('undo'); return true }, COMMAND_PRIORITY_CRITICAL)
    const offRedo = editor.registerCommand(REDO_COMMAND, () => { stepRef.current('redo'); return true }, COMMAND_PRIORITY_CRITICAL)
    return () => { offUndo(); offRedo() }
  }, [editor])
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
  onOptimizeChord,
  onUploadFiles,
  sentMessages,
  historyScope,
  onEditLastRequest,
  disabled,
  readOnly,
  sendOnEnter,
  showFullPastes,
  onEndUndoBurst,
}: Pick<LexicalComposerInputProps, 'blocks' | 'onBlocksChange' | 'onChange' | 'onSend' | 'onOptimizeChord' | 'onUploadFiles' | 'sentMessages' | 'historyScope' | 'onEditLastRequest' | 'disabled' | 'readOnly' | 'sendOnEnter' | 'showFullPastes' | 'onEndUndoBurst'>) {
  const [editor] = useLexicalComposerContext()
  const endUndoBurstRef = useRef(onEndUndoBurst)
  endUndoBurstRef.current = onEndUndoBurst
  const blocksRef = useRef(blocks)
  const rawPasteRef = useRef(false)
  const historyCursorRef = useRef<PromptHistoryCursor | null>(null)
  // Browsing belongs to the list it started in; another slot's list must not
  // resolve this cursor or restore this slot's saved draft.
  useEffect(() => { historyCursorRef.current = null }, [historyScope])
  // Shared IME latch (see useImeGuard.ts, ImeEnterClaimRatchet): on WebKit the
  // Enter that COMMITS a candidate arrives after `compositionend` with
  // `isComposing` already false, so the native flags alone cannot identify it.
  // The latch outlives them by the post-composition window; a private
  // flag-and-timer copy here is exactly the drift the ratchet pins.
  const imeLatchRef = useRef<ReturnType<typeof createImeLatch>>()
  if (!imeLatchRef.current) imeLatchRef.current = createImeLatch()
  blocksRef.current = blocks

  // A layout effect, not a passive one, for the same reason Lexical registers
  // its own plain-text handlers in one (usePlainTextSetup): the editor root
  // takes keydown from the moment ContentEditable's ref sets it, during this
  // same commit, while a passive effect waits for the scheduler's next task.
  // On the lazy first mount that task is a separate macrotask, so between the
  // two an Enter chord reaches only Lexical's default handler and inserts a
  // line break instead of sending or optimizing (#18193).
  useLayoutEffect(() => {
    const latch = imeLatchRef.current!
    // Track composition on the editor root itself, with the same stranded-latch
    // recovery the textarea binding carries: a composition abandoned without
    // `compositionend` (focus moves away, OS-level IME cancel) must not leave
    // the latch declining every later Enter.
    // Stable one-line delegations into the SHARED latch (useImeGuard's
    // createImeLatch) — the sanctioned wiring shape the ImeEnterClaimRatchet
    // scans for: every compositionstart subscriber must feed the shared latch.
    const onCompositionStart = () => latch.onCompositionStart()
    const onCompositionEnd = (e: CompositionEvent) => latch.onCompositionEnd(e.data)
    const onFocusChange = () => latch.reset()
    const rootListeners = editor.registerRootListener((root, prevRoot) => {
      if (prevRoot) {
        prevRoot.removeEventListener('compositionstart', onCompositionStart)
        prevRoot.removeEventListener('compositionend', onCompositionEnd)
        prevRoot.removeEventListener('focusout', onFocusChange)
        prevRoot.removeEventListener('focusin', onFocusChange)
      }
      if (root) {
        root.addEventListener('compositionstart', onCompositionStart)
        root.addEventListener('compositionend', onCompositionEnd)
        root.addEventListener('focusout', onFocusChange)
        root.addEventListener('focusin', onFocusChange)
      }
    })
    const unregisterModifier = editor.registerCommand(
      KEY_MODIFIER_COMMAND,
      event => {
        rawPasteRef.current = isRawPasteChord(event)
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
        if (onBlocksChange && !forceRaw && !showFullPastes && shouldCollapse(cleaned)) {
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
        // A focused token chip owns its Enter. The chip is non-editable DOM
        // inside the editor root, so its keydown reaches Lexical's root
        // listener before the chip's own React handler; without this claim the
        // editor would SEND THE DRAFT on the same keystroke that activates the
        // chip's preview. Claim the command (no lower-priority handler runs)
        // and leave the event untouched for the chip's activation handler.
        if (event.target instanceof HTMLElement && event.target.closest('[data-paste-seq]')) return true
        // A composing Enter commits an IME candidate, never sends. `claimKey`
        // consults the shared latch as well as the native flags, so WebKit's
        // post-`compositionend` commit keydown (isComposing already false) is
        // still recognized; it consumes the declined key per the useImeGuard
        // contract. Claim the command so no lower-priority handler inserts a
        // break from the same keypress.
        if (!latch.claimKey(event)) return true
        const commandKey = event.metaKey || event.ctrlKey
        // Cmd/Ctrl+Shift+Enter optimizes, before any send or newline rule, as
        // in the textarea's handler. The host gates the action itself.
        if (onOptimizeChord && commandKey && event.shiftKey) {
          event.preventDefault()
          onOptimizeChord()
          return true
        }
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
        // Auto-repeat of a held key is not a second send (it would confirm an
        // over-limit prompt the user never chose to send).
        if (!disabled && !readOnly && !event.repeat) onSend(sendOnEnter === 'enter' && commandKey)
        return true
      },
      COMMAND_PRIORITY_HIGH,
    )
    // Every new-line path (Shift+Enter, Enter in ctrl-enter mode, Ctrl+Enter in
    // enter-ctrl-newline mode, WebKit's beforeinput) arrives as one of these two
    // commands; the send key never does. `selectStart` is the caret-stays-put
    // break, which is not a new item.
    const continueList = (selectStart: boolean) =>
      !selectStart && !editor.isComposing() && !latch.isLatched() && $applyListLineBreak()
    const unregisterLineBreak = editor.registerCommand(
      INSERT_LINE_BREAK_COMMAND,
      continueList,
      COMMAND_PRIORITY_HIGH,
    )
    const unregisterParagraph = editor.registerCommand(
      INSERT_PARAGRAPH_COMMAND,
      () => continueList(false),
      COMMAND_PRIORITY_HIGH,
    )
    // Backspace (its keydown and the `deleteContentBackward` beforeinput)
    // arrives as a backward DELETE_CHARACTER_COMMAND. Android Chrome is the
    // exception: Lexical leaves a collapsed delete to the browser there,
    // because Chromium Android ignores preventDefault on that event, so this
    // handler never runs and the marker deletes one character at a time.
    // Claiming the beforeinput instead would delete the marker and then one
    // more character natively.
    const unregisterMarkerBackspace = editor.registerCommand(
      DELETE_CHARACTER_COMMAND,
      isBackward => isBackward && !editor.isComposing() && $applyListMarkerBackspace(() => endUndoBurstRef.current?.()),
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
      // ⌘↑ / Ctrl+↑: edit the last user message. Claimed only from an
      // empty composer — with content present the chord falls through so it
      // can never shadow multi-line caret movement (the same gate the plain
      // ↑ recall below enforces).
      if (direction === 'up' && (IS_MAC ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey) &&
        !event.altKey && !event.shiftKey && !event.isComposing) {
        if (!onEditLastRequest || $getRoot().getTextContent() !== '') return false
        event.preventDefault()
        onEditLastRequest()
        return true
      }
      if (!sentMessages?.length || event.isComposing || event.metaKey || event.ctrlKey ||
        event.altKey || event.shiftKey) return false
      const selection = $canonicalSelection()
      if (!selection || selection.start !== selection.end) return false
      const current = $getRoot().getTextContent()
      const cursor = livePromptHistoryCursor(historyCursorRef.current, current)
      historyCursorRef.current = cursor
      if (direction === 'up' && current !== '' && selection.start !== 0) return false
      if (direction === 'down' && selection.end !== current.length) return false
      const step = stepPromptHistory(sentMessages, cursor, direction === 'up' ? 'older' : 'newer', current)
      if (!step) return false
      event.preventDefault()
      historyCursorRef.current = step.cursor
      onChange(step.text)
      moveAfterRecall(step.text, direction === 'up' ? 'start' : 'end')
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
      unregisterLineBreak()
      unregisterParagraph()
      unregisterMarkerBackspace()
      unregisterArrowUp()
      unregisterArrowDown()
      rootListeners()
      // Drop any pending post-composition timer with the listeners so a stale
      // timer cannot write to the latch after teardown (useImeGuard contract).
      latch.reset()
    }
  }, [disabled, editor, onBlocksChange, onChange, onEditLastRequest, onOptimizeChord, onSend, onUploadFiles, readOnly, sendOnEnter, sentMessages, showFullPastes])

  return null
}

export default function LexicalComposerInput({
  value,
  blocks,
  onChange,
  onBlocksChange,
  showFullPastes = false,
  onSend,
  onOptimizeChord,
  ariaLabel,
  placeholder,
  disabled = false,
  readOnly = false,
  sendOnEnter = 'enter',
  spellCheck = true,
  inlineMarkdown = false,
  className = '',
  controlRef,
  editorRef,
  onReady,
  onSelectionChange,
  onHistoryStep,
  onEndUndoBurst,
  onUploadFiles,
  sentMessages,
  historyScope,
  onEditLastRequest,
}: LexicalComposerInputProps) {
  const initialValueRef = useRef({ value, blocks })
  const lastEmittedRef = useRef({ value, blocks })
  const initialConfig = useMemo(() => ({
    namespace: 'KiroCrewComposer',
    nodes: [PasteTokenNode],
    theme: INLINE_MARKDOWN_THEME,
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
              spellCheck={spellCheck}
              data-composer-input=""
              data-lexical-composer=""
              data-composer-typo=""
              className={`relative w-full min-h-[44px] max-h-[50vh] overflow-y-auto border-none bg-transparent text-text outline-hidden whitespace-pre-wrap break-words ${INPUT_TYPO}`}
            />
          }
          placeholder={
            /* Not a real `::placeholder`, so it carries the same hook as the
               editor: on a coarse pointer both get the 16px floor together and
               the overlay stays metric-identical to the text it stands in for. */
            <div data-composer-typo="" data-composer-placeholder="" className={`pointer-events-none absolute inset-0 overflow-hidden text-muted ${INPUT_TYPO}`}>
              {placeholder}
            </div>
          }
          ErrorBoundary={LexicalErrorBoundary}
        />
        <HistoryPlugin />
        {onHistoryStep && <HostHistoryPlugin onHistoryStep={onHistoryStep} />}
        <InlineMarkdownPlugin enabled={inlineMarkdown} />
        <ComposerControlPlugin
          controlRef={controlRef}
          onReady={onReady}
          onSelectionChange={onSelectionChange}
        />
        {editorRef && <EditorRefPlugin editorRef={editorRef} />}
        <OnChangePlugin onChange={handleChange} ignoreSelectionChange />
        <ControlledValuePlugin value={value} blocks={blocks} lastEmittedRef={lastEmittedRef} />
        <EditableStatePlugin editable={!disabled && !readOnly} />
        <MacLineEdgePlugin />
        <InteractionPlugin
          blocks={blocks}
          onBlocksChange={onBlocksChange}
          showFullPastes={showFullPastes}
          onChange={onChange}
          onSend={onSend}
          onOptimizeChord={onOptimizeChord}
          onUploadFiles={onUploadFiles}
          sentMessages={sentMessages}
          historyScope={historyScope}
          onEditLastRequest={onEditLastRequest}
          disabled={disabled}
          readOnly={readOnly}
          sendOnEnter={sendOnEnter}
          onEndUndoBurst={onEndUndoBurst}
        />
      </div>
    </LexicalComposer>
  )
}
