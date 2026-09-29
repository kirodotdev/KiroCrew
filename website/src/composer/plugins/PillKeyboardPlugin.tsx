import { useEffect } from 'react'
import {
  $getSelection,
  $isElementNode,
  $isNodeSelection,
  $isRangeSelection,
  COMMAND_PRIORITY_HIGH,
  KEY_DOWN_COMMAND,
  KEY_ARROW_LEFT_COMMAND,
  KEY_ARROW_RIGHT_COMMAND,
  KEY_BACKSPACE_COMMAND,
  KEY_DELETE_COMMAND,
  KEY_ENTER_COMMAND,
  type LexicalNode,
} from 'lexical'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'

import { $isPasteBlockNode } from '../nodes/PasteBlockNode'

/**
 * Single-keypress atomic delete of a pill, arrow keys that step over a
 * node-selected pill, and a guard so typing never replaces one.
 *
 * BACKSPACE/DELETE: if the current selection is a NodeSelection containing a
 * pill, remove every selected pill. Otherwise, on a collapsed caret adjacent to
 * a pill (backspace → previous sibling; delete → next sibling), remove that pill
 * in one keypress. Any other case returns false so PlainTextPlugin handles it.
 *
 * (Copy/cut expansion lives in the host shell, which owns the clipboard.)
 */
export default function PillKeyboardPlugin() {
  const [editor] = useLexicalComposerContext()

  useEffect(() => {
    // NOTE: command listeners already run inside an editor update. A nested
    // `editor.update()` here would be DEFERRED until the outer dispatch ends —
    // i.e. it would run AFTER PlainTextPlugin's own handler had already deleted
    // a character, see a caret now at offset 0 next to a pill, and remove the
    // pill as well (one Backspace deleting a character AND the paste).
    const removeAdjacentPill = (dir: 'backward' | 'forward'): boolean => {
      const selection = $getSelection()

      // NodeSelection of pills → remove them all.
      if ($isNodeSelection(selection)) {
        const pills = selection.getNodes().filter($isPasteBlockNode)
        if (!pills.length) return false
        for (const pill of pills) pill.remove()
        return true
      }

      if (!$isRangeSelection(selection) || !selection.isCollapsed()) return false

      const anchor = selection.anchor
      const node = anchor.getNode()
      let target: LexicalNode | null = null

      if (anchor.type === 'text') {
        // Only when the caret sits at the very edge of the text node.
        const atStart = anchor.offset === 0
        const atEnd = anchor.offset === node.getTextContentSize()
        if (dir === 'backward' && atStart) target = node.getPreviousSibling()
        else if (dir === 'forward' && atEnd) target = node.getNextSibling()
      } else {
        // Element point: offset is a child index within the paragraph.
        const para = node
        const idx = anchor.offset
        const children = $isElementNode(para) ? para.getChildren() : []
        if (dir === 'backward') target = children[idx - 1] ?? null
        else target = children[idx] ?? null
      }

      if (target && $isPasteBlockNode(target)) {
        target.remove()
        return true
      }
      return false
    }

    // When we handle the key we MUST preventDefault: otherwise the browser's
    // native deletion still fires a beforeinput and Lexical deletes one more
    // character on top of the pill.
    const onDeleteKey = (dir: 'backward' | 'forward') => (event: KeyboardEvent) => {
      // A focused chip owns its own removal. Claim the command before consulting
      // Lexical's stale caret so one keypress cannot remove a second pill.
      if (event.target instanceof Element && event.target.closest('[data-paste-seq]')) {
        event.preventDefault()
        return true
      }
      const handled = removeAdjacentPill(dir)
      if (handled) event.preventDefault()
      return handled
    }
    const unregisterBackspace = editor.registerCommand(KEY_BACKSPACE_COMMAND, onDeleteKey('backward'), COMMAND_PRIORITY_HIGH)
    const unregisterDelete = editor.registerCommand(KEY_DELETE_COMMAND, onDeleteKey('forward'), COMMAND_PRIORITY_HIGH)
    // Arrow keys while a pill is NODE-selected: PlainTextPlugin only handles
    // RangeSelections, so the caret could never leave a selected pill. ← parks
    // the caret before it, → after it (and the ring clears with the selection).
    const escapePill = (dir: 'left' | 'right') => (event: KeyboardEvent) => {
      const sel = $getSelection()
      if (!$isNodeSelection(sel)) return false
      const pills = sel.getNodes().filter($isPasteBlockNode)
      if (!pills.length) return false
      const target = dir === 'left' ? pills[0] : pills[pills.length - 1]
      if (dir === 'left') target.selectPrevious()
      else target.selectNext(0, 0)
      event.preventDefault()
      return true
    }
    // Typing while a pill is node-selected (e.g. right after a click) must NEVER
    // replace it: with a node selection Lexical's beforeinput bails out and the
    // browser's native "replace selected DOM" would swallow the whole paste.
    // Park the caret after the pill first; the keystroke then inserts there.
    const unregisterTyping = editor.registerCommand(
      KEY_DOWN_COMMAND,
      (event: KeyboardEvent) => {
        if (event.ctrlKey || event.metaKey || event.altKey || event.key.length !== 1) return false
        const sel = $getSelection()
        if (!$isNodeSelection(sel)) return false
        const pills = sel.getNodes().filter($isPasteBlockNode)
        if (!pills.length) return false
        pills[pills.length - 1].selectNext(0, 0)
        return false // not handled: the character now inserts at the new caret
      },
      COMMAND_PRIORITY_HIGH,
    )
    // Enter on a node-selected pill opens its preview (Escape/click work already).
    const unregisterEnter = editor.registerCommand(
      KEY_ENTER_COMMAND,
      (event: KeyboardEvent | null) => {
        const sel = $getSelection()
        if (!$isNodeSelection(sel)) return false
        const pill = sel.getNodes().find($isPasteBlockNode)
        if (!pill) return false
        event?.preventDefault()
        const el = editor.getElementByKey(pill.getKey())?.querySelector('[data-paste-seq]') as HTMLElement | null
        if (el) el.click()
        return true
      },
      COMMAND_PRIORITY_HIGH,
    )
    const unregisterLeft = editor.registerCommand(KEY_ARROW_LEFT_COMMAND, escapePill('left'), COMMAND_PRIORITY_HIGH)
    const unregisterRight = editor.registerCommand(KEY_ARROW_RIGHT_COMMAND, escapePill('right'), COMMAND_PRIORITY_HIGH)

    return () => {
      unregisterBackspace()
      unregisterDelete()
      unregisterLeft()
      unregisterRight()
      unregisterTyping()
      unregisterEnter()
    }
  }, [editor])

  return null
}
