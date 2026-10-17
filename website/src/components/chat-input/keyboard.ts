import { useCallback, useEffect, useRef } from 'react'
import { consumeComposerRelease } from '../../pages/chat/composerFocus'
import { isTouchDevice } from '../../utils/isTouchDevice'
import { IS_MAC } from '../../hooks/useKeyboardShortcuts'
import { activeElementIsEditable, isEditableTarget } from '../../utils/editableTarget'
import type { useImeGuard } from '../../hooks/useImeGuard'
import type { SendMode } from '../../pages/chat/ChatSettings'
import { isRawPasteChord } from '../composerPastePolicy'
import type { ComposerControl } from '../composerControl'
import type { ComposerVoiceInputProps } from '../../chat-core/composer/Composer'
import type { usePromptHistory } from './draftHistory'
import type { PromptHistoryItem } from '../composerPromptHistory'
import type { PasteBlock } from '../../utils/pasteTokens'
import type { MentionKeyMods } from './props'
import { applyTextareaListBreak } from './listContinuation'

/* The composer's keyboard and focus: autofocus on a session switch, the
   global `/` shortcut, the textarea's keydown (raw paste, undo, token keys,
   optimize, send, history recall, in that order), and the editor change
   handlers that turn typed text into picker triggers. */
export function useComposerFocus({ autoFocusKey, disabled, isMobile, composerControl, lexicalControlRevision, typedCommandMenus, composerCollapsed, expandComposer }: {
  autoFocusKey?: string | null
  disabled: boolean
  isMobile: boolean
  composerControl: () => ComposerControl | null
  lexicalControlRevision: number
  typedCommandMenus: boolean
  composerCollapsed: boolean
  expandComposer: () => void
}) {
  // Auto-focus textarea when the active session changes (autoFocusKey).
  // Track the previous key in a ref so the effect only acts on real key
  // transitions — `disabled` and `isMobile` are in the dep array to keep the
  // closure fresh, but a flip in either (e.g. AI finishes responding -> disabled
  // goes true -> false) MUST NOT steal focus while the user reads or scrolls.
  //
  // Also bail on touch devices: programmatic .focus() there pops the on-screen
  // keyboard, so merely tapping a session would cover half the screen before the
  // user has decided to type. `isMobile` (viewport width < 768px) already covers
  // portrait phones, but it's a LAYOUT signal — it misses tablets and phones in
  // landscape (≥768px), which are still touch. `isTouchDevice()` (coarse pointer
  // / no hover) is the precise keyboard-popping predicate. It's called inline,
  // not in the dep array, because a device's touch capability is effectively
  // static for the session (unlike `disabled`/`isMobile`, which flip at runtime).
  //
  // IMPORTANT: bail on `disabled || isMobile` BEFORE advancing the ref. If a
  // session switch lands while disabled=true (e.g. the user picks a session that
  // is currently stopping), advancing the ref here would consume the focus
  // opportunity — when disabled later flips false the effect re-runs but the
  // key check matches and bails. Holding the ref preserves the pending focus
  // until the gate clears.
  //
  // The active-element check IS placed after the ref update — that's a "decline
  // and don't retry" condition (if the user is typing in the agent picker, we
  // shouldn't come back later and steal focus once they switch back).
  const prevAutoFocusKeyRef = useRef<typeof autoFocusKey>(undefined)
  useEffect(() => {
    if (autoFocusKey == null || autoFocusKey === prevAutoFocusKeyRef.current) {
      prevAutoFocusKeyRef.current = autoFocusKey
      return
    }
    // A keyboard-driven switch released the composer (macOS chord chaining —
    // see releaseComposerForKeyboardSwitch): consume the one-shot and skip
    // this transition's autofocus entirely. The ref advances so the
    // disabled-retry path cannot resurrect the skipped focus later.
    if (consumeComposerRelease()) {
      prevAutoFocusKeyRef.current = autoFocusKey
      return
    }
    if (disabled || isMobile || isTouchDevice()) return
    const control = composerControl()
    if (!control) return
    prevAutoFocusKeyRef.current = autoFocusKey
    if (activeElementIsEditable()) return
    control.focus()
  }, [autoFocusKey, disabled, isMobile, composerControl, lexicalControlRevision])

  // Global "/" shortcut to focus chat input (like GitHub, YouTube, Slack).
  // Only the primary command composer claims it: with a second instance
  // mounted (the side panel), two document-level listeners would contend and
  // the last-registered one would silently win the focus.
  useEffect(() => {
    if (!typedCommandMenus) return
    const onSlashFocus = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return
      if (isEditableTarget(e)) return
      e.preventDefault()
      // `/` is an explicit "I want to type" gesture, so it outranks the collapse
      // and brings the box back (expandComposer focuses it on the next frame).
      //
      // The autoFocusKey effect just above deliberately does NOT do this. It
      // fires on every session SWITCH, which is navigation rather than typing
      // intent, so expanding there would make a deliberate, persisted preference
      // appear to undo itself while the user browses. Genuine post-create intent
      // is still covered: it arrives through `focusComposer`, which asks a
      // collapsed composer to return before giving up.
      if (composerCollapsed) { expandComposer(); return }
      // Focus through the engine-neutral control so the gesture lands in
      // whichever composer is live (textarea or the opt-in Lexical editor).
      composerControl()?.focus()
    }
    document.addEventListener('keydown', onSlashFocus)
    return () => document.removeEventListener('keydown', onSlashFocus)
  }, [typedCommandMenus, composerCollapsed, expandComposer, composerControl])
}

export function useComposerKeyDown({ rawPasteRef, handleUndoKey, endUndoBurst, handleTokenKey, onMentionKey, promptOptimizer, connected, optimizePrompt, sendOnEnter, onChange, valueFromUserRef, optimizingRef, fireComposer, ime, sentMessages, onEditLastRequest, anyPickerOpenRef, promptHistory, valueRef, inputRef, pasteBlocksRef }: {
  rawPasteRef: React.MutableRefObject<boolean>
  handleUndoKey: (e: React.KeyboardEvent<HTMLTextAreaElement>) => boolean
  endUndoBurst: () => void
  handleTokenKey: (e: React.KeyboardEvent<HTMLTextAreaElement>) => boolean
  onMentionKey?: (text: string, selStart: number, selEnd: number, key: string, mods: MentionKeyMods) => { value: string; caret: number } | null
  promptOptimizer: boolean
  connected: boolean
  optimizePrompt: () => void
  sendOnEnter: SendMode
  onChange: (v: string) => void
  valueFromUserRef: React.MutableRefObject<boolean>
  optimizingRef: React.MutableRefObject<boolean>
  fireComposer: (alternate?: unknown) => void
  ime: ReturnType<typeof useImeGuard>
  sentMessages?: PromptHistoryItem[]
  onEditLastRequest?: () => void
  anyPickerOpenRef: React.RefObject<boolean>
  promptHistory: ReturnType<typeof usePromptHistory>
  valueRef: React.MutableRefObject<string>
  inputRef: React.RefObject<HTMLTextAreaElement>
  pasteBlocksRef: React.RefObject<readonly PasteBlock[]>
}) {
  return useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Cmd/Ctrl+Shift+V (or Cmd+Option+Shift+V on macOS) → next paste inserts
    // full text inline (no chip collapse).
    // Self-clearing: any other keydown resets the flag so it only ever affects
    // the paste that immediately follows this exact shortcut. We do NOT
    // preventDefault — the browser still fires the paste event `handlePaste`
    // (chat-input/paste.ts) consumes.
    rawPasteRef.current = isRawPasteChord(e)
    // Undo / redo first (chat-input/draftHistory.ts): the gesture is the
    // composer's on every path, including a textarea whose native undo stack a
    // programmatic reset already wiped.
    if (handleUndoKey(e)) return
    // Atomic paste-token handling (chat-input/paste.ts) runs before
    // Enter/history, so edits on or around a token never reach the default
    // textarea handling.
    if (handleTokenKey(e)) return
    // Atomic file-mention handling: Backspace/Delete on or next to a staged
    // `@mention` removes the whole mention as one unit, so an edit never
    // leaves a half-reference whose chip then silently unstages (#14675). Runs
    // in the same slot as the paste-token atom, before Enter/history, so the
    // mention never reaches the default one-character edit. Skipped while the
    // optimizer runs: the textarea is readOnly then, so a Backspace next to a
    // mention must not unstage its chip and make the optimizer discard its
    // result on the now-shorter draft (crew-pr-reviewer).
    if (onMentionKey && !ime.isComposing(e) && !optimizingRef.current) {
      const ta = e.currentTarget
      const mods = { meta: e.metaKey, ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey }
      const edit = onMentionKey(valueRef.current, ta.selectionStart ?? 0, ta.selectionEnd ?? 0, e.key, mods)
      if (edit) {
        e.preventDefault()
        // Give the atomic delete its own undo entry: a short mention removed
        // within the typing burst would otherwise fold into it, so Ctrl+Z
        // would jump past the text typed before it instead of restoring just
        // the mention (matches applyTextareaListBreak / removeFileEndingUndoBurst).
        endUndoBurst()
        // Mark this as a real user edit, not a parent-driven draft restore, so
        // useUndoHistory records an undo entry for it. Without this, switching
        // between two slots whose drafts are byte-identical leaves the history
        // "settling" flag set (the value never changed, so that effect does not
        // re-run to clear it), and this unmarked onChange would reseed history
        // at the post-delete value — Ctrl+Z could then not restore the removed
        // mention or its attachment (GPT 6.1 review). Matches the paste-token
        // atom, which sets the same ref before its onChange.
        valueFromUserRef.current = true
        onChange(edit.value)
        requestAnimationFrame(() => inputRef.current?.setSelectionRange(edit.caret, edit.caret))
        return
      }
    }

    // Cmd+Shift+Enter (or Ctrl+Shift+Enter) → optimize prompt.
    // Gated on `promptOptimizer` like the Optimize button and plus-menu row:
    // a host that opted out (e.g. the side panel) has no optimize affordance,
    // so the combo falls through to ordinary Enter/Shift+Enter handling there
    // instead of rewriting a draft the surface meant to treat literally.
    // preventDefault always fires when the combo is detected so the browser's
    // default Enter behavior (newline insert) doesn't leak through when the
    // gateway is offline. The action itself is gated on `connected` to match
    // the disabled-state on the Optimize button.
    if (promptOptimizer && e.key === 'Enter' && (e.metaKey || e.ctrlKey) && e.shiftKey) {
      e.preventDefault()
      if (connected) optimizePrompt()
      return
    }
    // Mode: enter-ctrl-newline — Ctrl/Cmd+Enter inserts newline, Enter sends
    if (sendOnEnter === 'enter-ctrl-newline' && e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault()
      const ta = e.currentTarget
      // The new line this key makes still continues a markdown list unless
      // the IME owns this Enter; that path keeps the existing plain newline.
      if (!ime.isComposing(e) && applyTextareaListBreak(ta, pasteBlocksRef.current ?? [], endUndoBurst)) return
      const start = ta.selectionStart
      const end = ta.selectionEnd
      const val = ta.value
      onChange(val.slice(0, start) + '\n' + val.slice(end))
      requestAnimationFrame(() => { ta.selectionStart = ta.selectionEnd = start + 1 })
      return
    }
    const sendKey = sendOnEnter === 'ctrl-enter'
      ? (e.key === 'Enter' && (e.metaKey || e.ctrlKey))
      : (e.key === 'Enter' && !e.shiftKey)
    if (sendKey && !e.defaultPrevented) {
      // The key is ours as soon as it matches the send binding, so claim it before
      // deciding what to do with it — `claimEnter` suppresses the default and returns
      // false for an Enter the IME is committing. Every early return below therefore
      // leaves the draft untouched instead of gaining a newline, which is what the
      // browser does with an Enter nobody consumed.
      // The send itself is gated on `connected` to match the Send button's disabled
      // state, and skipped while a prompt optimization owns the draft.
      // While the composer is busy, Enter follows the split-button mode:
      // steer (default) acts on the text now; queue defers it.
      if (!ime.claimEnter(e)) return
      if (optimizingRef.current) return
      // A held-down key's auto-repeat is not a second send: it would confirm an
      // over-limit prompt the user never chose to send.
      if (e.repeat) return
      // ⌘↩ / Ctrl+Enter while the busy split is showing performs the OTHER
      // action for this send (steer ↔ queue) — the Claude Code / Codex gesture.
      // Only in the `enter` send mode: in `ctrl-enter` the modified Enter IS the
      // send key, and in `enter-ctrl-newline` the user gave it to newline (that
      // branch returned above). Idle, the modified Enter is a plain send, as it
      // always was. The flip lands in `fireComposer`, which ignores it whenever
      // the split is not available, so this cannot steer a non-steerable slot.
      const alternate = sendOnEnter === 'enter' && (e.metaKey || e.ctrlKey)
      if (connected) fireComposer(alternate)
      return
    }
    // ⌘↑ / Ctrl+↑: edit the last user message. Fires only from an
    // EMPTY composer (the same gate the plain-↑ recall below uses, so it can
    // never shadow multi-line caret movement), and not while a picker menu
    // owns the composer or IME composition is in flight.
    if (
      e.key === 'ArrowUp' && (IS_MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey) && !e.altKey && !e.shiftKey &&
      onEditLastRequest && valueRef.current === '' &&
      !anyPickerOpenRef.current && !ime.isComposing(e)
    ) {
      e.preventDefault()
      onEditLastRequest()
      return
    }
    // Prompt history: ↑/↓ cycles through prior user messages.
    // Ignore when IME composing, no history, modifier keys, or when
    // slash-command / file-picker / skill-picker menus are open (they own ↑/↓).
    if (
      !sentMessages?.length ||
      anyPickerOpenRef.current ||
      ime.isComposing(e) ||
      e.metaKey || e.ctrlKey || e.altKey || e.shiftKey
    ) return
    promptHistory.recall(e, { sentMessages, current: valueRef.current, onChange, inputRef })
  }, [rawPasteRef, handleUndoKey, endUndoBurst, handleTokenKey, onMentionKey, promptOptimizer, connected, optimizePrompt, sendOnEnter, onChange, valueFromUserRef, optimizingRef, fireComposer, ime, sentMessages, onEditLastRequest, anyPickerOpenRef, promptHistory, valueRef, inputRef, pasteBlocksRef])
}

/** The editor's change handlers. Both mark the edit as the user's (the undo
 *  recorder tells it apart from a draft restore), hand the text to the host,
 *  open whichever picker the text at the caret calls for, and publish the
 *  caret the dictation splice reads. */
export function useEditorInput({ onChange, valueFromUserRef, openPickersForText, recordCaret, lexicalControlRef, voiceCaretRef }: {
  onChange: (v: string) => void
  valueFromUserRef: React.MutableRefObject<boolean>
  openPickersForText: (text: string, before: string) => void
  recordCaret: () => void
  lexicalControlRef: React.RefObject<ComposerControl>
  voiceCaretRef: Partial<ComposerVoiceInputProps>['voiceCaretRef']
}) {
  const handleTextareaChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    valueFromUserRef.current = true // real DOM edit, not a parent-driven draft restore
    const val = e.target.value; onChange(val)
    openPickersForText(val, val.slice(0, e.target.selectionStart ?? val.length))
    recordCaret()
  }, [onChange, openPickersForText, recordCaret, valueFromUserRef])
  const handleLexicalChange = useCallback((nextValue: string) => {
    valueFromUserRef.current = true
    onChange(nextValue)
    const selection = lexicalControlRef.current?.getSelection()
    const caret = selection?.start ?? nextValue.length
    openPickersForText(nextValue, nextValue.slice(0, caret))
    if (selection && voiceCaretRef) voiceCaretRef.current = selection
  }, [onChange, openPickersForText, voiceCaretRef, valueFromUserRef, lexicalControlRef])
  return { handleTextareaChange, handleLexicalChange }
}
