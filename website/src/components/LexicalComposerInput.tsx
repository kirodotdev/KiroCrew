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
  $setCompositionKey,
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
import { createImeLatch } from '../hooks/useImeGuard'
import type { ComposerControl, ComposerRootHandle, ComposerSelection } from './composerControl'
import {
  clipboardFiles,
  hasPlainClipboardText,
  stripTrailingBlankLines,
} from './composerPastePolicy'
import {
  $createPasteBlockNode,
  $isPasteBlockNode,
  PasteBlockNode,
} from '../composer/nodes/PasteBlockNode'
import { DropGapNode } from '../composer/nodes/DropGapNode'
import PillsPlugin from '../composer/plugins/PillsPlugin'
import {
  countLines,
  findTokenRanges,
  makePasteId,
  nextSeqIn,
  pruneBlocks,
  shouldCollapse,
  splitDuplicateMarkers,
  type PasteBlock,
} from '../utils/pasteTokens'
import type { SendMode } from '../pages/chat/ChatSettings'

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
  onSend: () => void
  ariaLabel: string
  placeholder: string
  /** Hold the placeholder to ONE line with its cut tail faded out. ChatInput
   *  sets it for the sigil hint (`Message … (/command · @file · $skill)`), which
   *  is a label a narrow pane may cut; every status sentence (gateway offline,
   *  stopping, recording) and a caller's own placeholder keep wrapping so the
   *  reason is read whole. Mirrors the textarea's `::placeholder` rule (#13812);
   *  this overlay is a `<div>`, so the classes land on it directly. */
  placeholderOneLine?: boolean
  disabled?: boolean
  readOnly?: boolean
  sendOnEnter?: SendMode
  /** Draw the browser's red spellcheck underlines under the input. Default true
   *  (Chromium's own default); the Settings composer toggle drives it off. */
  spellCheck?: boolean
  className?: string
  controlRef?: React.MutableRefObject<ComposerControl | null>
  editorRef?: React.RefCallback<LexicalEditor> | React.RefObject<LexicalEditor | null | undefined>
  onReady?: () => void
  onSelectionChange?: (selection: ComposerSelection) => void
  onUploadFiles?: (files: File[]) => void
  sentMessages?: string[]
  /** Stable identity of the chat slot whose draft this editor contains. */
  historyKey?: string | null
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
    paragraph.append($createPasteBlockNode(range.block))
    cursor = range.end
  }
  appendPlainText(value.slice(cursor), node => paragraph.append(node))
}

function seedHistoryBaseline(editor: LexicalEditor): void {
  editor.update(() => { $getRoot().markDirty() }, { tag: 'history-merge', discrete: true })
}

function clearAndSeedHistory(editor: LexicalEditor): void {
  editor.dispatchCommand(CLEAR_HISTORY_COMMAND, undefined)
  // The fresh baseline makes the first keystroke or recall undoable back to the
  // restored per-slot draft, matching the textarea composer's reseed behavior.
  seedHistoryBaseline(editor)
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
    blocks: $nodesOfType(PasteBlockNode).map(node => node.getBlock()),
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
  blocks,
  controlRef,
  onReady,
  onSelectionChange,
}: {
  blocks: PasteBlock[]
  controlRef?: React.MutableRefObject<ComposerControl | null>
  onReady?: () => void
  onSelectionChange?: (selection: ComposerSelection) => void
}) {
  const [editor] = useLexicalComposerContext()
  const blocksRef = useRef(blocks)
  blocksRef.current = blocks

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
      replaceText: (text) => {
        const referencedBlocks = pruneBlocks(text, blocksRef.current)
        editor.update(() => {
          $replaceComposerValue(text, referencedBlocks)
          $getRoot().selectEnd()
        }, { tag: 'history-push' })
        editor.focus()
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
    // Control seam on the editable root (`ComposerRootHandle`): the control plus
    // a value read and a caret insert, for callers that reach the composer
    // through the DOM hook instead of a ref — SideChat's seed nudge and the
    // test drivers (see test/helpers `composer*`). `composerHandleOf(root)`
    // is the typed accessor.
    const hook: ComposerRootHandle = {
      ...control,
      getValue: () => {
        let value = ''
        editor.getEditorState().read(() => { value = $getRoot().getTextContent() })
        return value
      },
      insertText: (text: string) => {
        editor.update(() => {
          // A never-focused editor has no selection: append at the end, the
          // way a user's first click-then-type lands.
          const selection = $isRangeSelection($getSelection()) ? $getSelection() : $getRoot().selectEnd()
          if (!$isRangeSelection(selection)) return
          if (text === '') selection.removeText()
          else selection.insertRawText(text)
        }, { discrete: true })
      },
    }
    const unregisterRoot = editor.registerRootListener(root => {
      if (root) (root as HTMLElement & { __composer?: ComposerRootHandle }).__composer = hook
    })
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
      unregisterRoot()
      if (controlRef?.current === control) controlRef.current = null
    }
  }, [controlRef, editor, onReady, onSelectionChange])

  return null
}

function ControlledValuePlugin({
  value,
  blocks,
  historyKey,
  lastEmittedRef,
  onChange,
  onBlocksChange,
}: {
  value: string
  blocks: PasteBlock[]
  historyKey?: string | null
  lastEmittedRef: React.MutableRefObject<{ value: string; blocks: PasteBlock[] }>
  onChange: (value: string) => void
  onBlocksChange?: (blocks: PasteBlock[]) => void
}) {
  const [editor] = useLexicalComposerContext()
  const previousHistoryKeyRef = useRef(historyKey)
  const settlingRef = useRef(false)
  // The host value at the moment the settle flag was armed: the previous slot's
  // draft. Only a host value that differs from it while still equalling the
  // editor's last emission is a genuine editor echo.
  const settleBaselineRef = useRef<{ value: string; blocks: PasteBlock[] } | null>(null)

  useEffect(() => {
    // HistoryPlugin appears before ControlledValuePlugin in the composer tree,
    // so its effect has registered history before this mount seed runs. Defer
    // one microtask so the seed lands as its own commit after the initial value.
    let active = true
    queueMicrotask(() => {
      if (active) seedHistoryBaseline(editor)
    })
    return () => { active = false }
  }, [editor])

  useEffect(() => {
    // Invariant: every host sync is an explicit history push. The only clears
    // live here: a history-key change, and the first host value that settles
    // that slot switch (the restored draft, which the host commits separately
    // after the key). A genuine editor echo consumes the settle flag instead --
    // the user typed into an equal-valued slot, so there is no restore coming.
    // The residual edge where an unrelated host write arrives first
    // intentionally matches the textarea composer's slot-settling behavior.
    const historyChanged = historyKey !== previousHistoryKeyRef.current
    if (historyChanged) {
      previousHistoryKeyRef.current = historyKey
      clearAndSeedHistory(editor)
      settlingRef.current = true
      settleBaselineRef.current = { value, blocks }
    }
    const emitted = lastEmittedRef.current
    if (emitted.value === value && sameBlocks(emitted.blocks, blocks)) {
      // Equal to the editor's last emission: either an echo of an editor edit
      // or a re-run with nothing changed. This effect also re-runs when a
      // callback prop takes a new identity (the host passes an inline onChange
      // and re-renders per streamed chunk), and such a re-run can interleave
      // between the key change and the host's restore commit -- it must NOT
      // consume the flag, or the restore would be pushed as an undo step and
      // Ctrl+Z would cross back into the previous slot's draft. Only content
      // that moved away from the armed baseline proves a real editor echo.
      const baseline = settleBaselineRef.current
      if (settlingRef.current && baseline && (baseline.value !== value || !sameBlocks(baseline.blocks, blocks))) {
        settlingRef.current = false
        settleBaselineRef.current = null
      }
      return
    }
    let current = { value: '', blocks: [] as PasteBlock[] }
    editor.getEditorState().read(() => { current = $composerSnapshot() })
    if (current.value === value && sameBlocks(current.blocks, blocks)) return
    lastEmittedRef.current = { value, blocks }
    const settle = settlingRef.current
    settlingRef.current = false
    settleBaselineRef.current = null
    editor.update(() => $replaceComposerValue(value, blocks), {
      tag: [CONTROLLED_SYNC_TAG, settle ? 'history-merge' : 'history-push'],
      discrete: true,
    })
    if (settle) clearAndSeedHistory(editor)
    editor.getEditorState().read(() => { current = $composerSnapshot() })
    if (current.value === value && sameBlocks(current.blocks, blocks)) return
    lastEmittedRef.current = current
    if (current.value !== value) onChange(current.value)
    if (onBlocksChange && !sameBlocks(current.blocks, blocks)) onBlocksChange(current.blocks)
  }, [blocks, editor, historyKey, lastEmittedRef, onBlocksChange, onChange, value])

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
  if (!nodes.some($isPasteBlockNode)) return null
  return nodes.map(node => $isPasteBlockNode(node) ? node.getBlock().content : node.getTextContent()).join('')
}

function InteractionPlugin({
  value,
  blocks,
  onBlocksChange,
  onChange,
  onSend,
  onUploadFiles,
  sentMessages,
  disabled,
  readOnly,
  sendOnEnter,
  showFullPastes,
  historyKey,
}: Pick<LexicalComposerInputProps, 'value' | 'blocks' | 'onBlocksChange' | 'onChange' | 'onSend' | 'onUploadFiles' | 'sentMessages' | 'disabled' | 'readOnly' | 'sendOnEnter' | 'showFullPastes' | 'historyKey'>) {
  const [editor] = useLexicalComposerContext()
  const blocksRef = useRef(blocks)
  const rawPasteRef = useRef(false)
  const historyIndexRef = useRef(-1)
  // The draft parked while ↑/↓ browse sent messages. It carries its BLOCKS as
  // well as its text: a recalled message references no block, so browsing
  // prunes the editor's block list to [] (and the host follows), and the
  // restore must bring the draft's own records back or its markers decode as
  // literal text — the textarea path leaves `pasteBlocks` untouched across a
  // recall, and this composer has to match it.
  const historyDraftRef = useRef<{ value: string; blocks: PasteBlock[] }>({ value: '', blocks: [] })
  // Leave history mode the moment the host's value is no longer the sent message
  // being shown — the user edited it, the send pipeline cleared it, or the host
  // swapped in another slot's draft — and whenever the draft's owner changes.
  // The textarea path has the same rule (ChatInput's value effect); without it a
  // later ↓ would restore one slot's parked draft, pills included, into another.
  useEffect(() => {
    if (historyIndexRef.current !== -1 && value !== sentMessages?.[historyIndexRef.current]) {
      historyIndexRef.current = -1
      historyDraftRef.current = { value: '', blocks: [] }
    }
  }, [value, sentMessages])
  useEffect(() => {
    historyIndexRef.current = -1
    historyDraftRef.current = { value: '', blocks: [] }
  }, [historyKey])
  // Shared IME latch (see useImeGuard.ts, ImeEnterClaimRatchet): on WebKit the
  // Enter that COMMITS a candidate arrives after `compositionend` with
  // `isComposing` already false, so the native flags alone cannot identify it.
  // The latch outlives them by the post-composition window; a private
  // flag-and-timer copy here is exactly the drift the ratchet pins.
  const imeLatchRef = useRef<ReturnType<typeof createImeLatch>>()
  if (!imeLatchRef.current) imeLatchRef.current = createImeLatch()
  blocksRef.current = blocks

  useEffect(() => {
    const latch = imeLatchRef.current!
    // Track composition on the editor root itself, with the same stranded-latch
    // recovery the textarea binding carries: a composition abandoned without
    // `compositionend` (focus moves away, OS-level IME cancel) must not leave
    // the latch declining every later Enter.
    // Stable one-line delegations into the SHARED latch (useImeGuard's
    // createImeLatch) — the sanctioned wiring shape the ImeEnterClaimRatchet
    // scans for: every compositionstart subscriber must feed the shared latch.
    // Lexical mirrors the composition in its own key and drops EVERY keydown
    // while it is set (LexicalEvents.onKeyDown returns early on
    // `isComposing()`); `blur` is a pass-through command that never clears it,
    // so an abandoned composition would also leave the editor deaf. Recover
    // that state alongside the latch.
    const recoverEditorComposition = () => {
      if (editor.isComposing()) editor.update(() => { $setCompositionKey(null) }, { discrete: true })
    }
    const onCompositionStart = () => latch.onCompositionStart()
    const onCompositionEnd = () => latch.onCompositionEnd()
    const onFocusChange = () => { latch.reset(); recoverEditorComposition() }
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
        if (onBlocksChange && !forceRaw && !showFullPastes && shouldCollapse(cleaned)) {
          const block: PasteBlock = {
            id: makePasteId(),
            seq: nextSeqIn($getRoot().getTextContent(), blocksRef.current),
            lines: countLines(cleaned),
            content: cleaned,
          }
          selection.insertNodes([$createPasteBlockNode(block)])
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
      if (!$isNodeSelection(selection) || !selection.getNodes().some($isPasteBlockNode)) return false
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

    // `blocks` is the record set the incoming text may reference: the live list
    // for a recalled sent message (which references none of it, so the editor and
    // the host both end up at []), the parked draft's own list when the draft
    // comes back. Never the already-pruned live list for the restore — that is
    // how the draft's pill turned into literal marker text.
    const replaceRecalledText = (value: string, position: 'start' | 'end', blocks: PasteBlock[] = blocksRef.current) => {
      const referencedBlocks = pruneBlocks(value, blocks)
      blocksRef.current = referencedBlocks
      editor.update(() => {
        $replaceComposerValue(value, referencedBlocks)
        const offset = position === 'start' ? 0 : value.length
        const selection = $createRangeSelection()
        $setPointAtOffset(selection.anchor, offset)
        $setPointAtOffset(selection.focus, offset)
        $setSelection(selection)
      }, { tag: 'history-push' })
      editor.focus()
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
          historyDraftRef.current = { value: current, blocks: blocksRef.current }
          historyIndexRef.current = last
        } else if (index > 0) {
          historyIndexRef.current = index - 1
        }
        const recalled = sentMessages[historyIndexRef.current]
        event.preventDefault()
        replaceRecalledText(recalled, 'start')
        return true
      }
      const index = historyIndexRef.current
      if (index === -1 || selection.end !== current.length) return false
      event.preventDefault()
      if (index < last) {
        historyIndexRef.current = index + 1
        const recalled = sentMessages[historyIndexRef.current]
        replaceRecalledText(recalled, 'end')
      } else {
        historyIndexRef.current = -1
        const draft = historyDraftRef.current
        historyDraftRef.current = { value: '', blocks: [] }
        replaceRecalledText(draft.value, 'end', draft.blocks)
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
      rootListeners()
      // Drop any pending post-composition timer with the listeners so a stale
      // timer cannot write to the latch after teardown (useImeGuard contract).
      latch.reset()
    }
  }, [disabled, editor, onBlocksChange, onChange, onSend, onUploadFiles, readOnly, sendOnEnter, sentMessages, showFullPastes])

  return null
}

export default function LexicalComposerInput({
  value: hostValue,
  blocks: hostBlocks,
  onChange,
  onBlocksChange,
  showFullPastes = false,
  onSend,
  ariaLabel,
  placeholder,
  placeholderOneLine = false,
  disabled = false,
  readOnly = false,
  sendOnEnter = 'enter',
  spellCheck = true,
  className = '',
  controlRef,
  editorRef,
  onReady,
  onSelectionChange,
  onUploadFiles,
  sentMessages,
  historyKey,
}: LexicalComposerInputProps) {
  // The host's pair is canonicalised before it reaches the tree: a value that
  // holds the same marker twice (a restored draft, a small paste that contained
  // the marker text) would rehydrate as two pills sharing a seq, and a marker
  // resolves by seq alone — `expandAll` would send one block for both, dropping
  // the other's content once either had been edited. `splitDuplicateMarkers`
  // gives every later occurrence its own block + marker and returns the SAME
  // references when nothing changed. This happens here, in React, rather than
  // as a Lexical transform on the initial tree, because OnChangePlugin never
  // reports the initial commit or a `history-merge` update — the host would keep
  // the unrewritten value. (`PasteSeqInvariantPlugin` still guards pills created
  // INSIDE the editor, which do reach the host through OnChange.)
  const { text: value, blocks } = useMemo(() => splitDuplicateMarkers(hostValue, hostBlocks), [hostBlocks, hostValue])
  useEffect(() => {
    if (value !== hostValue) onChange(value)
    if (blocks !== hostBlocks) onBlocksChange?.(blocks)
  }, [blocks, hostBlocks, hostValue, onBlocksChange, onChange, value])

  const initialValueRef = useRef({ value, blocks })
  const lastEmittedRef = useRef({ value, blocks })
  const initialConfig = useMemo(() => ({
    namespace: 'KiroCrewComposer',
    nodes: [PasteBlockNode, DropGapNode],
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
        <PillsPlugin>
          <PlainTextPlugin
            contentEditable={
              <ContentEditable
                aria-label={ariaLabel}
                aria-multiline="true"
                spellCheck={spellCheck}
                data-composer-input=""
                data-composer-typo=""
                data-lexical-composer=""
                className={`relative w-full min-h-[44px] max-h-[50vh] overflow-y-auto border-none bg-transparent text-text outline-hidden whitespace-pre-wrap break-words ${INPUT_TYPO}`}
              />
            }
            placeholder={
              /* Not a real `::placeholder`, so it carries the same hook as the
                 editor: on a coarse pointer both get the 16px floor together and
                 the overlay stays metric-identical to the text it stands in for.
                 The hint is a label: in a narrow pane it stays on one line with
                 its tail faded (the textarea's `::placeholder` rule, on a div);
                 status sentences keep wrapping. Chromium paints no
                 `text-overflow` here either, so the cut edge fades. */
              <div
                data-composer-placeholder
                data-composer-typo=""
                className={`pointer-events-none absolute inset-0 overflow-hidden text-muted ${INPUT_TYPO} ${placeholderOneLine ? 'whitespace-nowrap [mask-image:linear-gradient(to_right,black_calc(100%-1.5rem),transparent)] [-webkit-mask-image:linear-gradient(to_right,black_calc(100%-1.5rem),transparent)]' : ''}`}
              >
                {placeholder}
              </div>
            }
            ErrorBoundary={LexicalErrorBoundary}
          />
        </PillsPlugin>
        <HistoryPlugin />
        <ComposerControlPlugin
          blocks={blocks}
          controlRef={controlRef}
          onReady={onReady}
          onSelectionChange={onSelectionChange}
        />
        {editorRef && <EditorRefPlugin editorRef={editorRef} />}
        <OnChangePlugin onChange={handleChange} ignoreSelectionChange />
        <ControlledValuePlugin
          value={value}
          blocks={blocks}
          historyKey={historyKey}
          lastEmittedRef={lastEmittedRef}
          onChange={onChange}
          onBlocksChange={onBlocksChange}
        />
        <EditableStatePlugin editable={!disabled && !readOnly} />
        <InteractionPlugin
          value={value}
          blocks={blocks}
          onBlocksChange={onBlocksChange}
          showFullPastes={showFullPastes}
          onChange={onChange}
          onSend={onSend}
          onUploadFiles={onUploadFiles}
          sentMessages={sentMessages}
          disabled={disabled}
          readOnly={readOnly}
          sendOnEnter={sendOnEnter}
          historyKey={historyKey}
        />
      </div>
    </LexicalComposer>
  )
}
