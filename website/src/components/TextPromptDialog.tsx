import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'

import { i18nT } from '../i18n/t'
import Modal from './Modal'
import { Btn, Input } from './ui'

interface TextPromptBaseOptions {
  title: string
  body?: string
  confirmLabel: string
}

interface RequiredTextPromptOptions extends TextPromptBaseOptions {
  allowEmpty?: false
  requiredMessage: string
}

interface OptionalTextPromptOptions extends TextPromptBaseOptions {
  allowEmpty: true
  requiredMessage?: never
}

export type TextPromptOptions = RequiredTextPromptOptions | OptionalTextPromptOptions

interface PendingTextPrompt {
  opts: TextPromptOptions
  resolve: (value: string | null) => void
  id: number
}

/** Detects renderer removal even when the hook's owner does not rerender. */
function DialogMountSignal({ onMountChange }: { onMountChange: (mounted: boolean) => void }) {
  useLayoutEffect(() => {
    onMountChange(true)
    return () => { onMountChange(false) }
  }, [onMountChange])
  return null
}

function TextPromptForm({ opts, formId, inputRef, onSubmit }: {
  opts: TextPromptOptions
  formId: string
  inputRef: RefObject<HTMLInputElement>
  onSubmit: (value: string) => void
}) {
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const inputId = useId()
  const errorId = useId()

  return (
    <form
      id={formId}
      className="flex flex-col gap-2"
      onSubmit={event => {
        event.preventDefault()
        if (!opts.allowEmpty && !value.trim()) {
          setError(opts.requiredMessage)
          return
        }
        onSubmit(value)
      }}
    >
      {opts.body && <label htmlFor={inputId} className="m-0 text-sm text-text">{opts.body}</label>}
      <Input
        id={inputId}
        ref={inputRef}
        aria-label={opts.body ? undefined : opts.title}
        value={value}
        aria-invalid={error ? true : undefined}
        aria-describedby={error != null ? errorId : undefined}
        onChange={event => {
          setValue(event.target.value)
          setError(null)
        }}
      />
      {error != null && (
        <p id={errorId} role="alert" className="m-0 text-sm text-danger">{error}</p>
      )}
    </form>
  )
}

/** Resolves the untrimmed text, or null on dismissal, replacement, owner unmount or renderer removal. */
export function useTextPrompt(): {
  textPrompt: (opts: TextPromptOptions) => Promise<string | null>
  textPromptDialog: ReactNode
} {
  const [pending, setPending] = useState<PendingTextPrompt | null>(null)
  const pendingRef = useRef<PendingTextPrompt | null>(null)
  // Modal needs the last content while its closing animation runs.
  const lastPromptRef = useRef<PendingTextPrompt | null>(null)
  const nextPromptIdRef = useRef(0)
  const dialogMountedRef = useRef(false)
  const dialogMountGenerationRef = useRef(0)
  const ownerMountedRef = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const formId = useId()
  const promptId = pending?.id

  // The owner effect runs after Modal's focus trap, so the input takes focus last.
  useEffect(() => {
    if (promptId !== undefined) inputRef.current?.focus({ preventScroll: true })
  }, [promptId])

  const textPrompt = useCallback((opts: TextPromptOptions) => new Promise<string | null>(resolve => {
    if (!ownerMountedRef.current) {
      resolve(null)
      return
    }
    pendingRef.current?.resolve(null)
    const next: PendingTextPrompt = { opts, resolve, id: ++nextPromptIdRef.current }
    pendingRef.current = next
    lastPromptRef.current = next
    setPending(next)
  }), [])

  const settle = useCallback((value: string | null) => {
    pendingRef.current?.resolve(value)
    pendingRef.current = null
    setPending(null)
  }, [])

  useLayoutEffect(() => {
    ownerMountedRef.current = true
    return () => {
      ownerMountedRef.current = false
      pendingRef.current?.resolve(null)
      pendingRef.current = null
    }
  }, [])

  const handleDialogMountChange = useCallback((mounted: boolean) => {
    const generation = ++dialogMountGenerationRef.current
    dialogMountedRef.current = mounted
    if (mounted) return
    // A microtask distinguishes final removal from StrictMode's same-commit remount.
    queueMicrotask(() => {
      if (!ownerMountedRef.current || dialogMountGenerationRef.current !== generation) return
      if (pendingRef.current) settle(null)
    })
  }, [settle])

  // An omitted renderer never mounts the signal, so the owner must settle it.
  useEffect(() => {
    if (pending && !dialogMountedRef.current) settle(null)
  })

  const displayed = pending ?? lastPromptRef.current
  const textPromptDialog = displayed ? (
    <>
      {pending && <DialogMountSignal onMountChange={handleDialogMountChange} />}
      <Modal
        open={!!pending}
        onClose={() => settle(null)}
        title={displayed.opts.title}
        maxWidth={440}
        footer={
          <>
            <Btn type="button" onClick={() => settle(null)}>
              {i18nT('components.confirmDialog.cancel')}
            </Btn>
            <Btn primary type="submit" form={formId}>
              {displayed.opts.confirmLabel}
            </Btn>
          </>
        }
      >
        <TextPromptForm
          key={displayed.id} opts={displayed.opts} formId={formId}
          inputRef={inputRef} onSubmit={settle}
        />
      </Modal>
    </>
  ) : null

  return { textPrompt, textPromptDialog }
}
