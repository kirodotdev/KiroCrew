import { useCallback, useRef } from 'react'
import type React from 'react'
import { i18nT } from '../../i18n/t'
import type { ComposerControl } from '../composerControl'

/* The first follow-up option as a grey suggestion in an EMPTY composer, which
 * Tab writes into the box as a draft (nothing is sent). Desktop only: a touch
 * keyboard has no Tab key, so there the chips above the box stay the one way in.
 *
 * The suggestion is the box's placeholder, so it needs no overlay that would have
 * to track the textarea and the rich-text editor separately, and it disappears on
 * the first typed character exactly as a placeholder does. Tab is taken in the
 * CAPTURE phase on the wrapper both editors sit in, so it wins over the list
 * indent and the focus move only while the suggestion is showing. An empty box has
 * no list line to indent, and Escape then Tab still moves focus, as it does on a
 * draft made of list lines (chat-input/keyboard.ts). */
export function useFollowUpGhost({ options, value, enabled, onUserEdit, composerControl, claimKey }: {
  options?: readonly string[]
  value: string
  /** False on touch input, and whenever the composer is locked, offline or busy
   *  with another mode, so the suggestion never replaces a placeholder that says why. */
  enabled: boolean
  /** Writes the draft as a USER edit, so the undo history records the fill and
   *  one Ctrl/Cmd+Z clears it again. */
  onUserEdit: (v: string) => void
  composerControl: () => ComposerControl | null
  /** The composer's IME latch (`useImeGuard().claimKey`): an IME cycles candidates with Tab. */
  claimKey: (e: React.KeyboardEvent) => boolean
}) {
  const suggestion = enabled && value === '' && options && options.length > 0 ? options[0] : null
  const escapedRef = useRef(false)
  const onKeyDownCapture = useCallback((e: React.KeyboardEvent) => {
    // The wrapper also holds sibling controls (the staged quote's buttons): their keys are theirs.
    const root = composerControl()?.getRootElement()
    if (!root || !(e.target instanceof Node) || !root.contains(e.target)) return
    const escaped = escapedRef.current
    escapedRef.current = e.key === 'Escape'
    if (!suggestion || escaped || e.key !== 'Tab') return
    if (e.shiftKey || e.metaKey || e.ctrlKey || e.altKey) return
    if (!claimKey(e)) return
    e.preventDefault()
    e.stopPropagation()
    onUserEdit(suggestion)
    // After the editor has the new value, so the caret lands after the text.
    requestAnimationFrame(() => composerControl()?.setSelection(suggestion.length, suggestion.length, { focus: true }))
  }, [suggestion, onUserEdit, composerControl, claimKey])
  const placeholder = suggestion ? i18nT('components.chatInput.follow_up_tab_placeholder', { option: suggestion }) : ''
  return { placeholder, onKeyDownCapture }
}
