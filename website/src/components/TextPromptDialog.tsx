import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'

import { i18nT } from '../i18n/t'
import Modal from './Modal'
import { Btn, Input } from './ui'

interface TextPromptBaseOptions {
  /** Short question or instruction shown as the dialog title and input label. */
  title: string
  /** Restates the action that accepts the typed value. */
  confirmLabel: string
  /** Hint shown in the empty field, e.g. the value a blank answer stands for. */
  placeholder?: string
}

/** A blank answer is refused with the caller's message, which names what to enter. */
interface RequiredTextPromptOptions extends TextPromptBaseOptions {
  allowEmpty?: false
  requiredMessage: string
}

/** A blank or whitespace-only answer is accepted and resolves as typed. */
interface OptionalTextPromptOptions extends TextPromptBaseOptions {
  allowEmpty: true
  requiredMessage?: never
}

export type TextPromptOptions = RequiredTextPromptOptions | OptionalTextPromptOptions

interface PendingTextPrompt {
  opts: TextPromptOptions
  resolve: (value: string | null) => void
  value: string
  error: ReactNode | null
}

/**
 * `textPrompt(opts)` resolves to the submitted text, or `null` when the prompt
 * is dismissed, replaced or unmounted. Render `textPromptDialog` once in the
 * component that owns the prompt.
 */
export function useTextPrompt(): {
  textPrompt: (opts: TextPromptOptions) => Promise<string | null>
  textPromptDialog: ReactNode
} {
  const [pending, setPending] = useState<PendingTextPrompt | null>(null)
  const pendingRef = useRef<PendingTextPrompt | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const errorId = useId()
  const openOpts = pending?.opts

  // Runs after Modal's focus trap, which moves focus to the first focusable
  // element (the ✕) and so overrides the input's own autofocus.
  useEffect(() => {
    if (openOpts) inputRef.current?.focus({ preventScroll: true })
  }, [openOpts])

  const textPrompt = useCallback((opts: TextPromptOptions) => new Promise<string | null>(resolve => {
    pendingRef.current?.resolve(null)
    const next: PendingTextPrompt = {
      opts,
      resolve,
      value: '',
      error: null,
    }
    pendingRef.current = next
    setPending(next)
  }), [])

  const settle = useCallback((value: string | null) => {
    pendingRef.current?.resolve(value)
    pendingRef.current = null
    setPending(null)
  }, [])

  useEffect(() => () => {
    pendingRef.current?.resolve(null)
    pendingRef.current = null
  }, [])

  const update = useCallback((change: Partial<Pick<PendingTextPrompt, 'value' | 'error'>>) => {
    const current = pendingRef.current
    if (!current) return
    const next = { ...current, ...change }
    pendingRef.current = next
    setPending(next)
  }, [])

  const submit = useCallback(() => {
    const current = pendingRef.current
    if (!current) return
    const value = current.value
    if (!current.opts.allowEmpty && !value.trim()) {
      update({ error: current.opts.requiredMessage })
      return
    }
    settle(value)
  }, [settle, update])

  const textPromptDialog = pending ? (
    <Modal
      open
      onClose={() => settle(null)}
      title={pending.opts.title}
      wrapTitle
      maxWidth={440}
      footer={
        <>
          <Btn type="button" onClick={() => settle(null)}>
            {i18nT('components.confirmDialog.cancel')}
          </Btn>
          <Btn primary type="submit" form="text-prompt-dialog-form">
            {pending.opts.confirmLabel}
          </Btn>
        </>
      }
    >
      <form
        id="text-prompt-dialog-form"
        className="flex flex-col gap-2"
        onSubmit={event => {
          event.preventDefault()
          submit()
        }}
      >
        <Input
          ref={inputRef}
          aria-label={pending.opts.title}
          placeholder={pending.opts.placeholder}
          value={pending.value}
          aria-invalid={pending.error ? true : undefined}
          aria-describedby={pending.error != null ? errorId : undefined}
          onChange={event => update({ value: event.target.value, error: null })}
        />
        {pending.error != null && (
          <p id={errorId} role="alert" className="m-0 text-sm text-danger">{pending.error}</p>
        )}
      </form>
    </Modal>
  ) : null

  return { textPrompt, textPromptDialog }
}
