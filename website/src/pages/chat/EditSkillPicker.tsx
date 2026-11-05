import { useCallback, useEffect, useMemo, useRef, type ChangeEvent, type FocusEvent, type ReactElement, type RefObject } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useComposerPickers } from '../../chat-core/composer/pickers'
import { ComposerPickerMenus } from '../../chat-core/composer/PickerMenus'
import type { ComposerPickerSource } from '../../chat-core/composer/pickerSource'
import type { ComposerControl } from '../../components/composerControl'
import { usePickerSource } from '../../components/dashboardPickerSource'

/** What the edit field needs from its host to open the `$` skill menu: the
 *  chat slot, and the agent and project the composer scopes its skills by. */
export interface EditSkillPickerTarget {
  slotId: string
  agentName?: string
  project?: string
}

/** The handlers the picker puts on the edit field. */
export interface EditFieldBinding {
  onChange: (e: ChangeEvent<HTMLTextAreaElement>) => void
  onFocus?: () => void
  onBlur?: (e: FocusEvent<HTMLElement>) => void
}

/**
 * The `$` skill menu on a message's Edit & Resend field, the same menu and
 * token the composer inserts.
 *
 * Only the skill menu is offered. An `@` pick stages an attachment that a
 * resend does not carry, and `/` and `./` complete text the edit endpoint
 * sends as a plain prompt, so their menus would promise what the resend does
 * not do. The source is cut down to the skills read, which is the one rule
 * (`offers`) that decides whether a menu opens.
 *
 * The field itself stays the host's: `renderField` draws it with the handlers
 * here, so the bubble's auto-sizing grid still holds the textarea directly.
 */
export default function EditSkillPicker({ target, value, onValueChange, textareaRef, anchorRef, renderField }: {
  target: EditSkillPickerTarget
  value: string
  onValueChange: (next: string) => void
  textareaRef: RefObject<HTMLTextAreaElement>
  anchorRef: RefObject<HTMLElement>
  renderField: (bind: EditFieldBinding) => ReactElement
}) {
  const base = usePickerSource()
  const source = useMemo<ComposerPickerSource>(
    () => ('skills' in base ? { scope: base.scope, skills: base.skills } : { scope: base.scope }),
    [base],
  )
  const queryClient = useQueryClient()
  const composerControl = useCallback((): ComposerControl | null => {
    const textarea = textareaRef.current
    if (!textarea) return null
    return {
      focus: () => textarea.focus(),
      getRootElement: () => textarea,
      getSelection: () => ({
        start: textarea.selectionStart ?? 0,
        end: textarea.selectionEnd ?? textarea.selectionStart ?? 0,
      }),
      setSelection: (start, end = start, options) => {
        const max = textarea.value.length
        textarea.setSelectionRange(Math.min(start, max), Math.min(end, max))
        if (options?.focus) textarea.focus()
      },
    }
  }, [textareaRef])
  const pickers = useComposerPickers({
    source,
    project: target.project,
    typedCommandMenus: true,
    value,
    onChange: onValueChange,
    composerControl,
    queryClient,
    slotId: target.slotId,
    agentName: target.agentName,
  })
  const { openPickersForText, prefetchSkills, closePickers } = pickers
  const onChange = useCallback((e: ChangeEvent<HTMLTextAreaElement>) => {
    const next = e.target.value
    onValueChange(next)
    openPickersForText(next, next.slice(0, e.target.selectionStart ?? next.length))
  }, [onValueChange, openPickersForText])
  // The open menu reads Enter and Escape off the whole document, so it closes
  // when focus lands anywhere but the field or the menus' own controls (the
  // Retry button, the consent dialog): Enter in the main composer then sends
  // that draft instead of picking into this one. The menus may be portaled, so
  // the wrapper below marks their focus event through React's tree, which runs
  // before this document listener sees the same event. Each event is judged on
  // its own, so nothing goes stale when a focused control unmounts.
  const menuFocusEvent = useRef<Event | null>(null)
  useEffect(() => {
    const onFocusIn = (e: Event) => {
      if (e.target !== textareaRef.current && e !== menuFocusEvent.current) closePickers()
    }
    document.addEventListener('focusin', onFocusIn)
    return () => document.removeEventListener('focusin', onFocusIn)
  }, [closePickers, textareaRef])
  // A click on empty space moves focus to nothing, which fires no focusin. The
  // window losing focus also blurs with no next target, but leaves the field as
  // the active element, so that keeps the menu.
  const onBlur = useCallback((e: FocusEvent<HTMLElement>) => {
    if (e.relatedTarget === null && document.activeElement !== textareaRef.current) closePickers()
  }, [closePickers, textareaRef])
  return (
    <>
      {renderField({ onChange, onFocus: prefetchSkills, onBlur })}
      <div
        className="contents"
        onFocusCapture={e => { menuFocusEvent.current = e.nativeEvent }}
      >
        <ComposerPickerMenus
          pickers={pickers}
          value={value}
          onChange={onValueChange}
          composerAnchorRef={anchorRef}
          sendOnEnter="enter"
          project={target.project}
          agentName={target.agentName}
        />
      </div>
    </>
  )
}
