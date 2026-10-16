import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import { i18nT } from '../i18n/t'
import Modal from './Modal'
import { Btn, Input } from './ui'

export interface TextPromptOptions {
  /** Short question or instruction shown as the dialog title and input label. */
  title: string
  /** Optional accessible input label when the title is not the best field name. */
  label?: string
  /** Restates the action that accepts the typed value. */
  confirmLabel: string
  /** The value shown when the prompt opens. */
  initialValue?: string
  /** Accept an empty or whitespace-only answer. Defaults to false. */
  allowEmpty?: boolean
  /** Optional input placeholder. */
  placeholder?: string
  /** Returns inline validation text, or nothing when the value is valid. */
  validate?: (value: string) => ReactNode | null | undefined
  /** Existing localized copy shown when a required value is blank. */
  emptyError?: ReactNode
  /** Compatibility alias for callers naming the same empty-value validation. */
  requiredMessage?: ReactNode
}

interface PendingTextPrompt {
  opts: TextPromptOptions
  resolve: (value: string | null) => void
  value: string
  error: ReactNode | null
}

/**
 * The themed, asynchronous replacement for one-line `window.prompt` calls.
 *
 * `promptText(opts)` resolves to the submitted text. Cancel, Escape, backdrop
 * dismissal, unmount, and replacement by another prompt all resolve `null`.
 * Render `textPromptDialog` once beside the component that owns the prompt.
 */
export function useTextPrompt(): {
  promptText: (opts: TextPromptOptions) => Promise<string | null>
  /** Alias matching the existing confirmation-hook naming convention. */
  textPrompt: (opts: TextPromptOptions) => Promise<string | null>
  textPromptDialog: ReactNode
  textPromptOpen: boolean
} {
  const [pending, setPending] = useState<PendingTextPrompt | null>(null)
  const pendingRef = useRef<PendingTextPrompt | null>(null)

  const promptText = useCallback((opts: TextPromptOptions) => new Promise<string | null>(resolve => {
    pendingRef.current?.resolve(null)
    const next: PendingTextPrompt = {
      opts,
      resolve,
      value: opts.initialValue ?? '',
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
    const error = !current.opts.allowEmpty && !value.trim()
      ? current.opts.emptyError ?? current.opts.requiredMessage ?? current.opts.title
      : current.opts.validate?.(value)
    if (error != null) {
      update({ error })
      return
    }
    settle(value)
  }, [settle, update])

  const textPromptDialog = pending ? (
    <Modal
      open
      onClose={() => settle(null)}
      title={pending.opts.title}
      maxWidth={440}
      footer={
        <>
          <Btn type="button" onClick={() => settle(null)}>
            {i18nT('components.confirmDialog.cancel')}
          </Btn>
          <Btn primary type="submit" form="text-prompt-dialog-form" onClick={submit}>
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
          autoFocus
          aria-label={pending.opts.label ?? pending.opts.title}
          placeholder={pending.opts.placeholder}
          value={pending.value}
          aria-invalid={pending.error ? true : undefined}
          onChange={event => update({ value: event.target.value, error: null })}
        />
        {pending.error != null && (
          <p role="alert" className="m-0 text-sm text-danger">{pending.error}</p>
        )}
      </form>
    </Modal>
  ) : null

  return { promptText, textPrompt: promptText, textPromptDialog, textPromptOpen: !!pending }
}
