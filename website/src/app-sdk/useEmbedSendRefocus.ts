/**
 * The ChatEmbed composer's keyboard focus across a send: a composer submit
 * (Enter or Send) gives focus back to the textarea once its send settles.
 *
 * The textarea is `disabled` while a send is in flight, and Send is disabled
 * once the draft clears. A browser moves focus to <body> when the focused
 * control becomes disabled, so without this the next keystrokes went nowhere.
 * Focus the user put somewhere else during the send stays there: only focus
 * that is on <body>, or still in the composer row, moves.
 *
 * The refocus runs in an effect, after the commit that enables the textarea
 * again. The mutation's own `onSettled` runs before that commit, while the
 * textarea is still disabled and cannot take focus.
 */
import { useCallback, useEffect, useRef, type MutableRefObject } from 'react'

export function useEmbedSendRefocus(pending: boolean, textareaRef: MutableRefObject<HTMLTextAreaElement | null>) {
  /** The composer row: the textarea, Send and the attach control. */
  const rowRef = useRef<HTMLDivElement | null>(null)
  const armedRef = useRef(false)
  useEffect(() => {
    if (pending || !armedRef.current) return
    armedRef.current = false
    const el = textareaRef.current
    if (!el) return
    const active = el.ownerDocument.activeElement
    if (!active || active === el.ownerDocument.body || rowRef.current?.contains(active)) el.focus()
  }, [pending, textareaRef])
  /** Called by a composer submit as it starts its send. A follow-up chip's
   *  send does not call it: focus was on the chip, not in the composer. */
  const armRefocus = useCallback(() => { armedRef.current = true }, [])
  return { rowRef, armRefocus }
}
