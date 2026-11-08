import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody, DialogFooter } from '../ui/dialog'
import { Btn } from '../ui'
import { guideConfirm } from '../../uiLocations/targetRegistry'
import { i18nT } from '../../i18n/t'

export interface DialogConfirmOptions {
  title: string
  body: ReactNode
  confirmLabel: string
}

interface Pending {
  opts: DialogConfirmOptions
  resolve: (confirmed: boolean) => void
}

/**
 * `useConfirm`'s promise API for a prompt raised from INSIDE a Radix `ui/dialog`.
 *
 * `useConfirm` renders a `Modal`, which is not a Radix layer: opening it moves
 * focus out of the Radix dialog and a press inside it lands outside that dialog,
 * so the dialog beneath reads both as a dismissal and closes. This renders the
 * prompt as a Radix `Dialog`, which stacks above the one that raised it: only
 * the top layer takes focus, outside presses and Escape. Cancel, Escape and the
 * X resolve `false`; only the confirm button resolves `true`.
 */
export function useDialogConfirm(): {
  confirm: (opts: DialogConfirmOptions) => Promise<boolean>
  confirmDialog: ReactNode
} {
  const [pending, setPending] = useState<Pending | null>(null)
  const pendingRef = useRef<Pending | null>(null)

  const confirm = useCallback((opts: DialogConfirmOptions) => new Promise<boolean>(resolve => {
    // A second ask while one is open answers the first "no".
    pendingRef.current?.resolve(false)
    const next = { opts, resolve }
    pendingRef.current = next
    setPending(next)
  }), [])

  const settle = useCallback((confirmed: boolean) => {
    pendingRef.current?.resolve(confirmed)
    pendingRef.current = null
    setPending(null)
  }, [])

  // An unmount while open answers "no" so the awaiting caller is released.
  useEffect(() => () => {
    pendingRef.current?.resolve(false)
    pendingRef.current = null
  }, [])

  const confirmDialog = (
    <Dialog open={pending !== null} onOpenChange={open => { if (!open) settle(false) }}>
      {pending && (
        <DialogContent maxWidth={440} className="z-[110]">
          <DialogHeader>
            <DialogTitle className="whitespace-normal">{pending.opts.title}</DialogTitle>
          </DialogHeader>
          <DialogBody>
            <div className="text-sm text-text">{pending.opts.body}</div>
          </DialogBody>
          <DialogFooter>
            <Btn onClick={() => settle(false)}>{i18nT('components.confirmDialog.cancel')}</Btn>
            {/* What a guide's destructive step waits for: this press, never a cancel. */}
            <Btn danger onClick={() => settle(true)} {...guideConfirm()}>{pending.opts.confirmLabel}</Btn>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  )
  return { confirm, confirmDialog }
}
