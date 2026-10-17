import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { PasteBlock } from '../utils/pasteTokens'
import { checkPromptLength, measurePrompt, sentPromptText } from './composerPromptLength'

/**
 * A confirming send inside this window after the hold is treated as the same
 * reflexive press (a double Enter or double click), so it stays held. The
 * window is measured from the last send that was swallowed, not from the first,
 * so a stream of repeats keeps holding for as long as it lasts.
 *
 * Sized for a held key rather than a double tap: the `e.repeat` guard in
 * `ChatInput` covers keydown on the textarea, but a Send / steer / queue button
 * that has focus is re-activated by the browser on the same auto-repeat, and
 * that arrives through a plain `onClick` with no repeat flag. Chrome's first
 * auto-repeat lands around 500 ms after the press, so the window has to sit
 * above it. A deliberate second press after releasing the key and waiting past
 * the window still sends.
 */
export const CONFIRM_DEAD_TIME_MS = 600

/**
 * Should a send of `value` be held for confirmation? Only a prompt over the
 * context window is held, and only until the user repeats the send for the
 * SAME prompt: `confirmedFor` is the sent text (paste chips expanded) the
 * first attempt was made on.
 */
export function holdOverLimitSend(
  value: string,
  blocks: readonly PasteBlock[],
  contextWindowTokens: number | undefined,
  confirmedFor: string | null,
): boolean {
  const sent = sentPromptText(value, blocks)
  if (confirmedFor !== null && confirmedFor === sent) return false
  return checkPromptLength(measurePrompt(sent), contextWindowTokens).level === 'over'
}

/**
 * Two-step send for an over-limit prompt. The first send is held and arms a
 * confirmation; a second send of the unchanged prompt goes through. Any edit
 * disarms it, including one that is later undone, and so does a change to a
 * paste block's content, because the arm is keyed on the text that would be
 * sent.
 *
 * Held rather than disabled: the token count is an estimate that errs high, so
 * a hard block could refuse a prompt the model would accept.
 */
export function useOverLimitSendConfirm(
  value: string,
  blocks: readonly PasteBlock[],
  contextWindowTokens: number | undefined,
  sessionId?: string | null,
) {
  const [armedFor, setArmedFor] = useState<string | null>(null)
  // Expanding the paste chips and trimming a multi-megabyte draft is the work
  // `PromptLengthNotice` keeps off the typing path with `useDeferredValue`, and
  // this hook renders inside `ChatInput`'s urgent render. Nothing here needs
  // the sent text while nothing is armed, so an unarmed keystroke expands
  // nothing; once armed, the only question is whether the draft still equals
  // the text it was armed on, and the first edit answers it and disarms.
  const sent = useMemo(
    () => (armedFor === null ? null : sentPromptText(value, blocks)),
    [armedFor, value, blocks],
  )
  const pending = armedFor !== null && armedFor === sent
  // A model or session change creates a new send context, even when the draft
  // text and estimated limit happen to be identical.
  useEffect(() => {
    setArmedFor(null)
  }, [contextWindowTokens, sessionId])
  // Drop the arm as soon as the prompt differs from what it was armed on, so
  // editing and then reverting the draft asks again instead of sending.
  useEffect(() => {
    if (armedFor !== null && armedFor !== sent) setArmedFor(null)
  }, [armedFor, sent])
  // Read through a ref so `intercept` keeps one identity across keystrokes and
  // does not re-create the send callback that closes over it.
  const latest = useRef({ value, blocks, contextWindowTokens, armedFor })
  latest.current = { value, blocks, contextWindowTokens, armedFor }
  const armedAt = useRef(0)
  /** Call before sending. True means "held: do not send now". */
  const intercept = useCallback((): boolean => {
    const cur = latest.current
    const now = Date.now()
    if (cur.armedFor !== null && now - armedAt.current < CONFIRM_DEAD_TIME_MS
      && cur.armedFor === sentPromptText(cur.value, cur.blocks)) {
      // Slide the window on: an auto-repeat train fires faster than the window
      // is wide, so measuring from the last swallowed send keeps a held key
      // held however long it is held for.
      armedAt.current = now
      return true
    }
    if (holdOverLimitSend(cur.value, cur.blocks, cur.contextWindowTokens, cur.armedFor)) {
      armedAt.current = now
      setArmedFor(sentPromptText(cur.value, cur.blocks))
      return true
    }
    if (cur.armedFor !== null) setArmedFor(null)
    return false
  }, [])
  return { pending, intercept }
}
