import { useCallback, useRef } from 'react'
import { applyListEdit, listLineBreakEdit, listMarkerBackspaceEdit } from '../composerListContinuation'
import { findTokenRanges, type PasteBlock } from '../../utils/pasteTokens'
import type { useImeGuard } from '../../hooks/useImeGuard'

/* Markdown list continuation for the textarea composer.

   Every new-line path in a textarea ends in a native `beforeinput` whose
   inputType is `insertLineBreak` (or `insertParagraph` on some Android
   keyboards): Shift+Enter, Enter in ctrl-enter mode, an iOS Return and an
   Android Gboard newline, whose keydown is `Unidentified` / keyCode 229 and
   so never matches an Enter check. Hooking the beforeinput, not the keydown,
   is what reaches all of them. The send key never gets here: its keydown is
   default-prevented, so no beforeinput follows. */

/** Continue or end the list item under a collapsed caret in `textarea`.
 *  Writes the value and caret directly, then fires an `input` event, so the
 *  caret moves synchronously and React's onChange sees an ordinary user edit. Returns
 *  false, leaving the textarea untouched, when the line is not a list item. */
export function applyTextareaListBreak(
  textarea: HTMLTextAreaElement,
  blocks: readonly PasteBlock[],
  endUndoBurst?: () => void,
): boolean {
  const { selectionStart, selectionEnd, value } = textarea
  if (selectionStart !== selectionEnd) return false
  const chips = findTokenRanges(value, [...blocks]).map(({ start, end }) => ({ start, end }))
  const edit = listLineBreakEdit(value, selectionStart, chips)
  if (!edit) return false
  // Its own undo step: without this a Return typed right after the item's text
  // merges into that typing burst, and one undo erases the whole item.
  endUndoBurst?.()
  // The prototype setter, not `textarea.value =`: React tracks the instance
  // setter, and a value written through it would make the `input` event below
  // look like no change, so onChange would never run.
  // Renumbering only rewrites digits after the caret, so the caret offset holds.
  setNativeValue(textarea, applyListEdit(value, edit))
  const caret = edit.start + edit.insert.length
  textarea.setSelectionRange(caret, caret)
  textarea.dispatchEvent(new Event('input', { bubbles: true }))
  return true
}

/** Remove the list marker in front of a collapsed caret in `textarea` in one
 *  Backspace (`listMarkerBackspaceEdit`). The same write path and undo step as
 *  the line break above; false leaves the ordinary one-character delete. */
export function applyTextareaListBackspace(
  textarea: HTMLTextAreaElement,
  blocks: readonly PasteBlock[],
  endUndoBurst?: () => void,
): boolean {
  const { selectionStart, selectionEnd, value } = textarea
  if (selectionStart !== selectionEnd) return false
  const chips = findTokenRanges(value, [...blocks]).map(({ start, end }) => ({ start, end }))
  const edit = listMarkerBackspaceEdit(value, selectionStart, chips)
  if (!edit) return false
  endUndoBurst?.()
  // Clearing an empty numbered item also moves the items below it back up;
  // those writes are all after the caret, so its offset holds.
  setNativeValue(textarea, applyListEdit(value, edit))
  textarea.setSelectionRange(edit.start, edit.start)
  textarea.dispatchEvent(new Event('input', { bubbles: true }))
  return true
}

/** True on Android, where Chromium can ignore preventDefault on a
 *  `deleteContentBackward` beforeinput. */
function isAndroid(): boolean {
  return typeof navigator !== 'undefined' && /Android/i.test(navigator.userAgent ?? '')
}

function setNativeValue(textarea: HTMLTextAreaElement, next: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
  if (setter) setter.call(textarea, next)
  else textarea.value = next
}

const NEW_LINE_INPUT_TYPES = new Set(['insertLineBreak', 'insertParagraph'])

/** List continuation for whichever textarea is mounted: `ref` is a callback
 *  ref that attaches the `beforeinput` listener (a ref, not an effect: the
 *  textarea remounts on composer collapse / expand and Lexical load failure
 *  without any prop an effect could key on), and `onKeyDown` goes on the
 *  textarea so the listener knows which keydown made the line break.
 *
 *  That keydown matters for one case the `beforeinput` cannot see on its own.
 *  On WebKit the Enter that commits an IME candidate arrives AFTER
 *  `compositionend` with `isComposing` false and keyCode 13, so only the
 *  composer's shared IME latch (`useImeGuard`) still knows it is the IME's;
 *  in ctrl-enter mode nothing claims that keydown, the browser's line break
 *  follows, and continuing the list there would inject a marker into a CJK
 *  draft. The latch alone is not the signal, though: Android Gboard composes
 *  every word, so a Return right after one lands inside the same window with a
 *  keydown of `Unidentified` / keyCode 229 — and that newline must continue
 *  the list. The discriminator is the keydown itself: an `Enter` (not keyCode
 *  229) that the guard still reports as composing is the WebKit commit. */
export function useTextareaListContinuation(
  blocksRef: React.RefObject<readonly PasteBlock[]>,
  ime: Pick<ReturnType<typeof useImeGuard>, 'isComposing'>,
  endUndoBurst: () => void,
) {
  const detachRef = useRef<(() => void) | null>(null)
  // The guard object is rebuilt every render; read it through a ref so the
  // callback ref below keeps its identity (a new identity would detach and
  // re-attach, and re-run every other ref it is composed with, each render).
  const imeRef = useRef(ime)
  imeRef.current = ime
  // Set by the keydown, read and cleared by the beforeinput it produces. Any
  // later keydown or beforeinput also clears it, so a commit Enter that made
  // no line break (claimed as a send) cannot decline an unrelated newline.
  const declineNextBreakRef = useRef(false)
  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Through the guard, never the raw flag (the `ImeEnterClaimRatchet` pins
    // that): the guard reads true for the latch or either native signal, so
    // with keyCode 229 ruled out what is left is the latch, or a mid-composition
    // Enter whose own beforeinput (composition text, or a composing newline)
    // is declined anyway and clears the note.
    const imeOwned = imeRef.current.isComposing(e) && e.keyCode !== 229
    declineNextBreakRef.current = imeOwned && e.key === 'Enter'
  }, [])
  const ref = useCallback((textarea: HTMLTextAreaElement | null) => {
    detachRef.current?.()
    detachRef.current = null
    declineNextBreakRef.current = false
    if (!textarea) return
    const onBeforeInput = (event: InputEvent) => {
      const commitEnter = declineNextBreakRef.current
      declineNextBreakRef.current = false
      // Backspace takes the same route. A composing delete edits the IME's
      // candidate, not the list. Android is left to the browser: Chromium
      // Android can ignore preventDefault on this event, so removing the
      // marker here would be followed by the browser's own one-character
      // delete and merge the line into the one above. The same fallback as
      // the Lexical composer: one character per press, never an overshoot.
      if (event.inputType === 'deleteContentBackward') {
        if (event.isComposing || !event.cancelable || isAndroid()) return
        if (applyTextareaListBackspace(textarea, blocksRef.current ?? [], endUndoBurst)) event.preventDefault()
        return
      }
      // A composing newline, or the WebKit commit Enter's, belongs to the IME;
      // it is not a new list item.
      if (commitEnter || event.isComposing || !NEW_LINE_INPUT_TYPES.has(event.inputType)) return
      if (applyTextareaListBreak(textarea, blocksRef.current ?? [], endUndoBurst)) event.preventDefault()
    }
    textarea.addEventListener('beforeinput', onBeforeInput)
    detachRef.current = () => textarea.removeEventListener('beforeinput', onBeforeInput)
  }, [blocksRef, endUndoBurst])
  return { ref, onKeyDown }
}
