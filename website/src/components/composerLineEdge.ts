import { useEffect } from 'react'
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext'
import {
  $createRangeSelectionFromDom,
  $setSelection,
  COMMAND_PRIORITY_HIGH,
  KEY_DOWN_COMMAND,
} from 'lexical'
import { isMacOSPlatform } from '../utils/platform'

/** Which end of the caret's visual line a key press targets. */
export type LineEdge = 'start' | 'end'

/** A claimed Home / End press: the target edge, and whether Shift extends the selection to it. */
export interface LineEdgeIntent {
  edge: LineEdge
  extend: boolean
}

type LineEdgeKeyEvent = Pick<
  KeyboardEvent,
  'key' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'isComposing' | 'keyCode'
>

/**
 * Map a keydown to a visual-line edge, or null when the key keeps its native
 * behaviour. On macOS a bare Home / End in a contenteditable scrolls the page
 * (or does nothing) instead of moving the caret; every other platform already
 * moves the caret, so only macOS is claimed. Shift extends the selection to
 * the edge instead of moving the caret. Any Alt / Cmd / Ctrl chord keeps its
 * own meaning, and a key that belongs to an IME composition (`isComposing` or
 * the 229 placeholder keyCode) is left to the IME.
 */
export function lineEdgeForKey(event: LineEdgeKeyEvent, mac: boolean): LineEdgeIntent | null {
  if (!mac) return null
  if (event.key !== 'Home' && event.key !== 'End') return null
  if (event.altKey || event.ctrlKey || event.metaKey) return null
  if (event.isComposing || event.keyCode === 229) return null
  return { edge: event.key === 'Home' ? 'start' : 'end', extend: event.shiftKey }
}

/**
 * Move the DOM caret to the start or end of the visual (wrapped) line it sits
 * on, or with `extend` move only the selection's focus there so the anchor
 * stays put. `lineboundary` is resolved by the browser's own layout, so a long
 * paragraph that wraps onto several rows moves within the current row only.
 * Returns false, leaving the DOM untouched, when the caret is not inside
 * `root` or the engine has no `Selection.modify`.
 */
export function moveDomCaretToLineEdge(root: HTMLElement, edge: LineEdge, extend = false): boolean {
  const selection = root.ownerDocument.defaultView?.getSelection() ?? null
  if (!selection || selection.rangeCount === 0 || typeof selection.modify !== 'function') return false
  if (!selection.focusNode || !root.contains(selection.focusNode)) return false
  selection.modify(extend ? 'extend' : 'move', edge === 'start' ? 'backward' : 'forward', 'lineboundary')
  scrollCaretIntoView(root, selection)
  return true
}

/** Scroll `root` the minimum needed so the selection's focus row is visible. */
function scrollCaretIntoView(root: HTMLElement, selection: Selection): void {
  if (!selection.focusNode) return
  const range = root.ownerDocument.createRange()
  range.setStart(selection.focusNode, selection.focusOffset)
  range.collapse(true)
  let rect: DOMRect | undefined = range.getBoundingClientRect()
  if (!rect.height) rect = range.getClientRects()[0]
  if (!rect || !rect.height) return
  const box = root.getBoundingClientRect()
  if (rect.top < box.top) root.scrollTop -= box.top - rect.top
  else if (rect.bottom > box.bottom) root.scrollTop += rect.bottom - box.bottom
}

/**
 * Lexical plugin: on macOS, Home / End move the caret to the edge of its
 * visual line (Shift+Home / Shift+End extend the selection there) and the
 * editor's own selection is set from the moved DOM range in the same update,
 * so the next keystroke acts on the new position.
 * Keys aimed at a focused chip or any other control inside the editor (the
 * event target is not the editable root) are left alone.
 */
export function MacLineEdgePlugin() {
  const [editor] = useLexicalComposerContext()
  useEffect(() => editor.registerCommand(
    KEY_DOWN_COMMAND,
    event => {
      const intent = lineEdgeForKey(event, isMacOSPlatform())
      if (!intent) return false
      const root = editor.getRootElement()
      if (!root || event.target !== root) return false
      if (!moveDomCaretToLineEdge(root, intent.edge, intent.extend)) return false
      event.preventDefault()
      const next = $createRangeSelectionFromDom(root.ownerDocument.defaultView?.getSelection() ?? null, editor)
      if (next) $setSelection(next)
      return true
    },
    COMMAND_PRIORITY_HIGH,
  ), [editor])
  return null
}
