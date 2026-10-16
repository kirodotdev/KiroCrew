import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { guideConfirm } from '../uiLocations/targetRegistry'

import Modal from './Modal'
import { Btn } from './ui'
import { i18nT } from '../i18n/t'

/**
 * Shared async confirmation surface, replacing `window.confirm`.
 *
 * Native `confirm` is synchronous: it freezes the renderer's event loop, so a
 * Quit event queued behind it fires the instant it dismisses — tearing the app
 * down before the follow-up request is sent. It also leaks the origin string
 * into an unthemeable OS sheet and cannot restate the action on its button.
 * This dialog is the themed, non-blocking replacement: the confirm button
 * restates the action instead of "OK", and the caller awaits a boolean.
 *
 * The confirm button is styled destructively by default, and the cancel label
 * is the shared one unless a caller names what staying means ("Keep editing").
 * Add further knobs only when a caller needs them.
 */
export interface ConfirmOptions {
  /** Short dialog title. A question restating the stakes reads best. */
  title: string
  /** Optional detail under the title, e.g. the resources being destroyed. */
  body?: ReactNode
  /** Restates the action ("Discard changes", "Destroy site") — never "OK". */
  confirmLabel: string
  /** Names what cancelling keeps ("Keep editing"). Defaults to the shared Cancel. */
  cancelLabel?: string
  /** Every earlier caller confirms a destructive act, so danger styling is the
   *  default. Pass `false` for a weighty but non-destructive confirm (a grant). */
  danger?: boolean
  /** Paints the confirm button as the page's primary action, for a confirm whose
   *  yes is the next step rather than a risk (an export the user asked for). */
  primary?: boolean
  /** `top` paints the prompt above a full-screen overlay (`z-[9999]`) the
   *  caller may be raising it from; see `Modal`'s `layer`. */
  layer?: 'dialog' | 'top'
}

interface PendingConfirm {
  opts: ConfirmOptions
  resolve: (confirmed: boolean) => void
}

/** Reports whether the rendered modal subtree exists in the committed DOM. */
function DialogMountSignal({ onMountChange }: { onMountChange: (mounted: boolean) => void }) {
  useLayoutEffect(() => {
    onMountChange(true)
    return () => { onMountChange(false) }
  }, [onMountChange])
  return null
}

/**
 * Promise-based confirm for one component: `confirm(opts)` resolves `true`
 * only when the user presses the confirm button; Cancel, Escape, the X
 * button, and a backdrop click all resolve `false`. Render `confirmDialog`
 * once in the component's JSX.
 *
 * Unlike `window.confirm`, the UI stays live while the dialog is open, so a
 * caller that reads mutable state after the `await` must re-read or capture
 * it — the answer can arrive an arbitrary time later. A component that owns a
 * document-level keyboard handler should skip it entirely while `confirmOpen`
 * is true: Escape would re-trigger the guard the dialog just dismissed, and a
 * save shortcut would persist the draft the user is about to discard.
 */
export function useConfirm(): {
  confirm: (opts: ConfirmOptions) => Promise<boolean>
  cancel: () => void
  confirmDialog: ReactNode
  confirmOpen: boolean
} {
  const [pending, setPending] = useState<PendingConfirm | null>(null)
  const [dialogPresent, setDialogPresent] = useState(false)
  const pendingRef = useRef<PendingConfirm | null>(null)
  const dialogMountedRef = useRef(false)
  const dialogMountGenerationRef = useRef(0)
  const ownerMountedRef = useRef(false)
  // Keeps the last options through the exit animation: Modal stays mounted
  // with open=false so AnimatePresence can play the dialog out, and it still
  // needs a title/footer to render while doing so.
  const lastOptsRef = useRef<ConfirmOptions | null>(null)

  const confirm = useCallback((opts: ConfirmOptions) => {
    return new Promise<boolean>(resolve => {
      // A second ask while one is open answers the first "no" rather than
      // leaking its promise (and its awaiting caller) forever.
      pendingRef.current?.resolve(false)
      const next = { opts, resolve }
      pendingRef.current = next
      lastOptsRef.current = opts
      setDialogPresent(true)
      setPending(next)
    })
  }, [])

  const settle = useCallback((confirmed: boolean) => {
    pendingRef.current?.resolve(confirmed)
    pendingRef.current = null
    setPending(null)
  }, [])

  useLayoutEffect(() => {
    ownerMountedRef.current = true
    return () => { ownerMountedRef.current = false }
  }, [])

  const handleDialogMountChange = useCallback((mounted: boolean) => {
    const generation = ++dialogMountGenerationRef.current
    dialogMountedRef.current = mounted
    if (mounted) return
    queueMicrotask(() => {
      if (!ownerMountedRef.current || dialogMountedRef.current || dialogMountGenerationRef.current !== generation) return
      setDialogPresent(false)
      if (pendingRef.current) settle(false)
    })
  }, [settle])

  // A question omitted by the hook owner never mounts the signal above. This
  // owner check handles that case; signal cleanup handles descendant-only
  // removal without waiting for the owner to render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (dialogMountedRef.current) return
    if (pending) {
      setDialogPresent(false)
      settle(false)
    } else if (dialogPresent) {
      setDialogPresent(false)
    }
  })

  // Keep parent modals inert until this dialog has actually left the DOM.
  const finishExit = useCallback(() => {
    if (!pendingRef.current) setDialogPresent(false)
  }, [])

  // An unmount while the dialog is open answers "no" so the awaiting caller
  // is released instead of suspended forever.
  useEffect(() => () => {
    pendingRef.current?.resolve(false)
    pendingRef.current = null
  }, [])

  const opts = pending?.opts ?? lastOptsRef.current
  const confirmDialog = opts ? (
    <Modal
      open={!!pending}
      onClose={() => settle(false)}
      title={opts.title}
      wrapTitle
      maxWidth={440}
      layer={opts.layer}
      onExitComplete={finishExit}
      footer={
        <>
          <DialogMountSignal onMountChange={handleDialogMountChange} />
          <Btn onClick={() => settle(false)}>
            {opts.cancelLabel ?? i18nT('components.confirmDialog.cancel')}
          </Btn>
          {/* What a guide's destructive step waits for: this press, never a cancel. */}
          <Btn danger={!opts.primary && opts.danger !== false} primary={opts.primary} onClick={() => settle(true)} {...guideConfirm()}>
            {opts.confirmLabel}
          </Btn>
        </>
      }
    >
      {/* Full-contrast body: this line carries the consequence ("permanently
          deletes bucket …"), which must not read quieter than the buttons.
          Text renders as a paragraph; an element body may carry block content
          (a list of what is lost), which a <p> cannot contain. */}
      {opts.body == null ? null : typeof opts.body === 'string'
        ? <p className="text-sm text-text m-0">{opts.body}</p>
        : <div className="text-sm text-text">{opts.body}</div>}
    </Modal>
  ) : null

  const cancel = useCallback(() => settle(false), [settle])

  return { confirm, cancel, confirmDialog, confirmOpen: dialogPresent }
}
