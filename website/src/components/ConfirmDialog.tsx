import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

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
 * The confirm button is always styled destructively and the cancel label is
 * always the shared one: every current caller confirms a destructive act, so
 * the knobs would ship without a consumer. Add them when a caller needs them.
 */
export interface ConfirmOptions {
  /** Short dialog title. A question restating the stakes reads best. */
  title: string
  /** Optional detail under the title, e.g. the resources being destroyed. */
  body?: ReactNode
  /** Restates the action ("Discard changes", "Destroy site") — never "OK". */
  confirmLabel: string
}

interface PendingConfirm {
  opts: ConfirmOptions
  resolve: (confirmed: boolean) => void
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
  confirmDialog: ReactNode
  confirmOpen: boolean
} {
  const [pending, setPending] = useState<PendingConfirm | null>(null)
  const pendingRef = useRef<PendingConfirm | null>(null)
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
      setPending(next)
    })
  }, [])

  const settle = useCallback((confirmed: boolean) => {
    pendingRef.current?.resolve(confirmed)
    pendingRef.current = null
    setPending(null)
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
      maxWidth={440}
      footer={
        <>
          <Btn onClick={() => settle(false)}>
            {i18nT('components.confirmDialog.cancel')}
          </Btn>
          <Btn danger onClick={() => settle(true)}>
            {opts.confirmLabel}
          </Btn>
        </>
      }
    >
      {/* Full-contrast body: this line carries the consequence ("permanently
          deletes bucket …"), which must not read quieter than the buttons. */}
      {opts.body != null ? <p className="text-sm text-text m-0">{opts.body}</p> : null}
    </Modal>
  ) : null

  return { confirm, confirmDialog, confirmOpen: !!pending }
}
