import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import type { useAppStore } from '../../store'
import { selectSlotMessages } from '../../store/chatSlice'
import { pruneBlocks, type PasteBlock } from '../../utils/pasteTokens'
import { i18nT } from '../../i18n/t'
import type { ComposerControl } from '../composerControl'

/* "Optimize prompt": the request, its slot binding, and the undoable
   write-back. The result is never sent; it lands in the draft of the slot
   that asked, or goes to `onOptimizeResult` when that slot is no longer the
   one on screen. */

export function usePromptOptimizer({ slotId, chatStore, valueRef, pasteBlocks, onChange, onOptimizeResult, lexicalComposer, lexicalLoadFailed, composerControl, inputRef, valueFromUserRef, optimizingRef, appendUndoBoundary }: {
  slotId: string | null
  chatStore: ReturnType<typeof useAppStore>
  valueRef: React.MutableRefObject<string>
  pasteBlocks: PasteBlock[]
  onChange: (v: string) => void
  onOptimizeResult?: (slotId: string | null, optimized: string) => void
  lexicalComposer: boolean
  lexicalLoadFailed: boolean
  composerControl: () => ComposerControl | null
  inputRef: React.RefObject<HTMLTextAreaElement>
  valueFromUserRef: React.MutableRefObject<boolean>
  /** Written here during render; the undo recorder and the keydown handler read it. */
  optimizingRef: React.MutableRefObject<boolean>
  appendUndoBoundary: (v: string) => void
}) {
  // Tracks the prior render's raw pending state so the completion effect can
  // record a single undo boundary when an optimize actually finishes (as
  // opposed to the scoped `optimizing` flipping off because the user switched
  // sessions mid-flight).
  const wasOptimizingRef = useRef(false)
  // The slot that initiated the in-flight optimize. Overlay / readOnly / pending
  // state is scoped to this slot so navigating to another session mid-optimize
  // dismisses the overlay here and only reveals it again when we return to the
  // originating session. Null when no optimize is in flight.
  const optimizeSlotRef = useRef<string | null>(null)
  // In-composer report of a rejected optimize request (the restored prompt
  // alone says nothing about why the optimizer did not run).
  const [optimizeError, setOptimizeError] = useState('')

  const setTextUndoable = useCallback((text: string) => {
    if (lexicalComposer && !lexicalLoadFailed) {
      valueFromUserRef.current = true
      onChange(text)
      requestAnimationFrame(() => composerControl()?.setSelection(text.length, text.length, { focus: true }))
      return
    }
    const el = inputRef.current
    if (!el) { onChange(text); return }
    el.readOnly = false
    el.focus()
    el.select()
    // Same reconciliation handlePaste does, for the same reason: execCommand's
    // boolean is not evidence. It is absent entirely on some engines, and iOS
    // Safari reports success on a <textarea> while leaving the field untouched.
    // Here the whole field was just select()ed, so an unverified failure leaves
    // the ORIGINAL prompt on screen with the optimizer's result discarded and
    // no error — indistinguishable from "the optimizer changed nothing".
    let inserted = false
    try {
      inserted = typeof document.execCommand === 'function' && document.execCommand('insertText', false, text)
    } catch { inserted = false }
    // Reconcile through the controlled value either way, exactly as handlePaste
    // does: after a real insert this is the same string the textarea's own
    // onChange already pushed up (React bails), while an insert React never saw
    // would be reverted to the stale `value` prop on the next render — the same
    // silent vanish by a different route. Marked user-driven so the undo
    // recorder treats it as an edit (a new boundary) rather than a
    // parent-driven draft restore, which is what keeps this "undoable".
    const nativeOk = inserted && el.value === text
    valueFromUserRef.current = true
    onChange(text)
    if (nativeOk) return // the native insert placed the caret itself
    requestAnimationFrame(() => {
      if (el && document.activeElement === el) el.setSelectionRange(text.length, text.length)
    })
  }, [onChange, lexicalComposer, lexicalLoadFailed, composerControl, valueFromUserRef, inputRef])

  const optimizeMutation = useMutation({
    onMutate: () => { setOptimizeError('') },
    mutationFn: async (
      { prompt, context, pastes }: {
        prompt: string
        context: string
        pastes?: Array<{ seq: number; content: string }>
        slotId: string | null
      },
    ) => {
      const resp = await fetch('/api/optimizer/optimize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-session-key': 'dashboard:ui' },
        credentials: 'same-origin',
        body: JSON.stringify({ prompt, context, pastes }),
      })
      if (!resp.ok) throw new Error('optimizer failed')
      return resp.json()
    },
    onSuccess: (data, variables) => {
      // Originating session is still the one on screen: write the result here,
      // undoable. The textarea stayed readOnly for the whole optimize on this
      // session so the value can't have diverged from what we sent; the
      // trim-guard defends against a stray whitespace-only mismatch or any
      // unforeseen divergence (drop rather than clobber).
      if (variables.slotId === slotId) {
        if (valueRef.current.trim() !== variables.prompt.trim()) return
        setTextUndoable(data.changed && data.optimized ? data.optimized : valueRef.current.trim())
        return
      }
      // The user navigated to a different session mid-optimize. Route the
      // result back to the session that started it instead of writing into the
      // session now on screen (wrong session) or dropping it (lost work). Fall
      // back to the original prompt when the optimizer returned no change.
      onOptimizeResult?.(variables.slotId, data.changed && data.optimized ? data.optimized : variables.prompt)
    },
    onError: (err, variables) => {
      // eslint-disable-next-line no-console -- surface prompt-optimizer failures to the dev console
      console.warn('optimizer failed', err)
      // The user must learn the optimizer failed: restoring the prompt alone is
      // indistinguishable from "the optimizer changed nothing". Shown on
      // whichever session is on screen — this composer instance is the
      // always-mounted surface; a notice gated on the originating slot would
      // stay hidden for a user who navigated away mid-optimize. The copy names
      // where the restore happened, so it never claims a change to a composer
      // the user is looking at that did not visibly change.
      setOptimizeError(variables.slotId === slotId
        ? i18nT('components.chatInput.optimize_failed')
        : i18nT('components.chatInput.optimize_failed_elsewhere'))
      // Same slot-routing split as onSuccess. On the originating session,
      // restore the original prompt in place; otherwise hand it back to that
      // session's draft so a failed optimize on a backgrounded session doesn't
      // leave stale readOnly text or vanish.
      if (variables.slotId === slotId) {
        if (valueRef.current.trim() !== variables.prompt.trim()) return
        setTextUndoable(valueRef.current.trim())
        return
      }
      onOptimizeResult?.(variables.slotId, variables.prompt)
    },
  })
  // Raw request lifecycle — true whenever a request is in flight, regardless of
  // which session is currently displayed.
  const optimizePending = optimizeMutation.isPending
  // Scoped view of that state: only "optimizing" while we're still showing the
  // slot that initiated it. Navigating to a different session dismisses the
  // overlay / readOnly / disabled state here; returning restores it. In grid
  // mode each pane has its own ChatInput + mutation, so slotId always matches
  // and this reduces to the raw pending flag.
  const optimizing = optimizePending && optimizeSlotRef.current === slotId
  optimizingRef.current = optimizing
  // Re-entrancy guard reads the RAW lifecycle: only one optimize may be in
  // flight per ChatInput instance. Without this, the button on a *different*
  // session (where scoped `optimizing` is false) could fire a second request
  // that clobbers the single mutation's in-flight state.
  const optimizePendingRef = useRef(false)
  optimizePendingRef.current = optimizePending

  // When an optimize completes, ensure its result is a single undo boundary.
  // The recording effect skips writes while `optimizing` is true; a single-shot
  // optimize lands after `optimizing` clears and is already recorded, but a
  // streaming optimize would otherwise leave the final value unrecorded — so
  // push one boundary here if the tip doesn't already hold it. Idempotent: if
  // the recording effect already captured it, the value-equality guard no-ops.
  //
  // Keyed on the RAW pending lifecycle (not the slot-scoped `optimizing`) and
  // fenced to the originating slot: switching sessions mid-flight flips scoped
  // `optimizing` off without the request finishing, and we must NOT record a
  // boundary against the session we navigated to. We only record when the
  // request truly settles while the originating slot is still displayed; the
  // request-diverged case is dropped by onSuccess/onError anyway.
  useEffect(() => {
    if (wasOptimizingRef.current && !optimizePending) {
      const originating = optimizeSlotRef.current
      optimizeSlotRef.current = null
      if (originating === slotId) appendUndoBoundary(valueRef.current)
    }
    wasOptimizingRef.current = optimizePending
  }, [optimizePending, slotId, appendUndoBoundary, valueRef])
  const { mutate: runOptimize } = optimizeMutation

  // The optimizer's context is the ONLY reader of this slot's message history,
  // and only when "Optimize prompt" is clicked. Subscribing here forced every
  // mounted composer to re-render on each streamed frame (Immer hands back a new
  // `state.messages` reference per flush, so the `===` selector always tripped),
  // which multiplied with N split panes. Read the slot's own messages at click
  // time instead — `selectSlotMessages` returns THIS pane's slot (falling back
  // to the active mirror when this pane IS active), which also fixes a bug where
  // a non-active pane sent the *active* pane's conversation as optimizer context.
  const optimizePrompt = useCallback(() => {
    const txt = valueRef.current.trim()
    // Guard on the RAW lifecycle so a second optimize can't start while one is
    // in flight — even from a different session where scoped `optimizing` reads
    // false (a single mutation backs this instance).
    if (!txt || optimizePendingRef.current) return
    // Pin the slot that owns this optimize so the overlay and the completion
    // handler stay bound to it across session switches.
    optimizeSlotRef.current = slotId
    // Read THIS pane's slot messages at click time (not via a live subscription),
    // so a non-active pane optimizes against its own conversation, not the active
    // pane's. When slotId is null (no SlotProvider / global composer) read the
    // active mirror, which is exactly what the old `s.chat.messages` subscription
    // returned; selectSlotMessages also falls back to that mirror for the active
    // slot, so the focused composer's behavior is preserved.
    const rootState = chatStore.getState()
    const slotMessages = slotId
      ? selectSlotMessages(rootState, slotId)
      : rootState.chat.messages
    const context = slotMessages
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .slice(-10)
      .map(m => (m.content || '').slice(0, 200))
      .join('\n')
    // Forward the full content behind each paste placeholder still present in
    // the draft, so the optimizer understands the paste without us expanding
    // the "[ Paste #N · M lines ]" token inline. The optimizer preserves the
    // tokens verbatim in its output, so pasteBlocks keeps mapping them back on
    // send. Only referenced blocks are sent (pruneBlocks drops stale ones).
    const referenced = pruneBlocks(txt, pasteBlocks)
    const pastes = referenced.map(b => ({ seq: b.seq, content: b.content }))
    runOptimize({ prompt: txt, context, pastes, slotId })
  }, [runOptimize, pasteBlocks, slotId, chatStore, valueRef])

  return { optimizeError, setOptimizeError, optimizePending, optimizing, optimizePrompt }
}
