import { useCallback, useEffect, useInsertionEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
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

function ConfirmControls({ question, onMountChange, onAnswer }: {
  question: PendingConfirm
  onMountChange: (question: PendingConfirm, surface: object, mounted: boolean) => void
  onAnswer: (confirmed: boolean, question: PendingConfirm, surface: object) => void
}) {
  const surface = useRef({}).current
  const { opts } = question
  useLayoutEffect(() => {
    onMountChange(question, surface, true)
    return () => { onMountChange(question, surface, false) }
  }, [question, surface, onMountChange])
  return (
    <>
      <Btn onClick={() => onAnswer(false, question, surface)}>
        {i18nT('components.confirmDialog.cancel')}
      </Btn>
      {/* What a guide's destructive step waits for: this press, never a cancel. */}
      <Btn danger={!opts.primary && opts.danger !== false} primary={opts.primary} onClick={() => onAnswer(true, question, surface)} {...guideConfirm()}>
        {opts.confirmLabel}
      </Btn>
    </>
  )
}

/**
 * `confirm(opts)` resolves `true` only when the user presses the confirm
 * button; Cancel, Escape, the X button and a backdrop click resolve `false`.
 * Render `confirmDialog` once in the component's JSX.
 * An ask also resolves `false` when its owner unmounts, a newer ask replaces
 * it, or its dialog is not rendered or stops being rendered, releasing callers
 * when no answer is possible.
 * An ask made while React holds the owner's update (a pending transition or
 * suspended ancestor) stays open until React commits, then appears normally.
 * `body` must render synchronously because suspension defers the dialog with
 * its owner.
 * The UI stays live while the dialog is open, so capture the confirmed target
 * before awaiting and re-read any other mutable state that must be current
 * when the answer arrives. A component that owns a document-level
 * keyboard handler should skip it while `confirmOpen` is true: Escape would
 * re-trigger the guard the dialog just dismissed, and a save shortcut would
 * persist the draft the user is about to discard.
 */
export function useConfirm(): {
  confirm: (opts: ConfirmOptions) => Promise<boolean>
  cancel: () => void
  confirmDialog: ReactNode
  confirmOpen: boolean
} {
  const [pending, setPending] = useState<PendingConfirm | null>(null)
  const pendingRef = useRef<PendingConfirm | null>(null)
  const ownerMountedRef = useRef(false)
  const ownerEffectMountedRef = useRef(false)
  const committedSurfaceRef = useRef<{ question: PendingConfirm; surface: object } | null>(null)
  // Exit animations still need the answered question's title and controls.
  const [lastQuestion, setLastQuestion] = useState<PendingConfirm | null>(null)

  const confirm = useCallback((opts: ConfirmOptions) => {
    return new Promise<boolean>(resolve => {
      if (!ownerMountedRef.current) {
        resolve(false)
        return
      }
      // Replacing a question must release its awaiting caller.
      pendingRef.current?.resolve(false)
      const next = { opts, resolve }
      pendingRef.current = next
      setLastQuestion(next)
      setPending(next)
    })
  }, [])

  const settle = useCallback((confirmed: boolean, question = pendingRef.current, surface?: object) => {
    const current = pendingRef.current
    if (!current || current !== question) return
    // Only the current mounted controls can approve, even after the question remounts.
    if (confirmed && (!ownerMountedRef.current
        || committedSurfaceRef.current?.question !== current
        || committedSurfaceRef.current.surface !== surface)) return
    current.resolve(confirmed)
    pendingRef.current = null
    setPending(null)
  }, [])

  // Descendant layout effects can ask before the owner's layout effects run.
  useInsertionEffect(() => {
    ownerMountedRef.current = true
    return () => {
      ownerMountedRef.current = false
      pendingRef.current?.resolve(false)
      pendingRef.current = null
    }
  }, [])

  const cancelMissingSurface = useCallback((question: PendingConfirm) => {
    // Portals and same-commit remounts can supply controls after a cleanup.
    queueMicrotask(() => {
      if (!ownerMountedRef.current || pendingRef.current !== question
          || committedSurfaceRef.current?.question === question) return
      settle(false, question)
      setLastQuestion(retained => retained === question ? null : retained)
    })
  }, [settle])

  // Suspense hides disconnect layout effects even if no controls ever commit.
  useLayoutEffect(() => () => {
    const question = pendingRef.current
    if (question) cancelMissingSurface(question)
  }, [cancelMissingSurface])

  // Hidden-tree deletion can skip insertion cleanup; passive effects still detach.
  useEffect(() => {
    ownerEffectMountedRef.current = true
    return () => {
      ownerEffectMountedRef.current = false
      queueMicrotask(() => {
        if (ownerEffectMountedRef.current) return
        ownerMountedRef.current = false
        pendingRef.current?.resolve(false)
        pendingRef.current = null
      })
    }
  }, [])

  const handleDialogMountChange = useCallback((question: PendingConfirm, surface: object, mounted: boolean) => {
    if (mounted) {
      committedSurfaceRef.current = { question, surface }
      return
    }
    const committed = committedSurfaceRef.current
    if (committed?.question !== question || committed.surface !== surface) return
    committedSurfaceRef.current = null
    if (pendingRef.current === question) cancelMissingSurface(question)
  }, [cancelMissingSurface])

  useEffect(() => {
    if (!pending || committedSurfaceRef.current?.question === pending) return
    cancelMissingSurface(pending)
  }, [pending, cancelMissingSurface])

  const finishExit = useCallback((question: PendingConfirm) => {
    setLastQuestion(retained => retained === question && !pendingRef.current ? null : retained)
  }, [])

  const question = pending ?? lastQuestion
  const opts = question?.opts
  const confirmDialog = question && opts ? (
    <Modal
      open={!!pending}
      onClose={() => settle(false, question)}
      title={opts.title}
      maxWidth={440}
      layer={opts.layer}
      onExitComplete={() => finishExit(question)}
      footer={<ConfirmControls question={question} onMountChange={handleDialogMountChange} onAnswer={settle} />}
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

  return { confirm, cancel, confirmDialog, confirmOpen: !!pending }
}
