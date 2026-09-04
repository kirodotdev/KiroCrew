import React, { useEffect, useCallback, useId, useRef } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { X } from 'lucide-react'

import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { useDocumentImeLatch } from '../hooks/useImeGuard'
import { i18nT } from '../i18n/t'
interface ModalProps {
  /** Whether the modal is open */
  open: boolean
  /** Called when the modal should close (backdrop click, Escape, or X button) */
  onClose: () => void
  /** Modal title displayed in the header */
  title: React.ReactNode
  /**
   * Explicit accessible dialog name. Only needed when the rendered title does
   * not read as a usable name on its own (icon-only, or text assembled from
   * fragments). Left unset, the dialog is named by its own rendered title via
   * `aria-labelledby`, so no dialog is ever anonymous.
   */
  ariaLabel?: string
  /** Optional content pinned at the bottom of the modal */
  footer?: React.ReactNode
  /** Optional actions rendered in the header (right side, before the close button) */
  headerActions?: React.ReactNode
  /** Max width of the modal (default: 640px) */
  maxWidth?: number
  /** Fixed height (e.g. '70vh'). If not set, modal sizes to content up to max-h-[90vh] */
  height?: string
  /** Framer Motion layoutId for card-to-modal expand animation. When set, the modal
   *  morphs from a matching layoutId element instead of using scale+opacity. */
  layoutId?: string
  /** When true, the ACCIDENTAL dismissal paths (backdrop click, Escape) are
   *  ignored; the explicit ones (X button, and any footer Cancel the caller
   *  renders) still close. Set it while a modal holds unsaved user input, so
   *  grazing the backdrop cannot silently destroy a part-filled form. */
  guardAccidentalDismiss?: boolean
  /** Modal content */
  children: React.ReactNode
}

/** The dialog surface itself — everything except the backdrop. */
type ModalDialogProps = Omit<ModalProps, 'open'> & { maxWidth: number }

const SPRING = { type: 'spring' as const, stiffness: 500, damping: 35 }

/**
 * Mounted ONLY while the modal is open, which is what makes the shared focus
 * trap correct here: `useDialogFocusTrap` keys focus-in / focus-restore to its
 * own mount/unmount, and `Modal` itself stays mounted across open+closed (call
 * sites render `<Modal open={false} …/>`). Wiring the hook into `Modal` would
 * therefore capture the restore target at page load and move focus into a
 * dialog that is not on screen.
 */
function ModalDialog({ onClose, title, ariaLabel, footer, headerActions, maxWidth, height, layoutId, children }: ModalDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const dismiss = useCallback(() => onClose(), [onClose])
  const reactId = useId()
  const titleId = `${reactId}-title`

  // Focus in on open, restore to whatever opened the dialog on close, Escape
  // dismissal, and the Tab/Shift+Tab trap — one implementation shared with the
  // hand-rolled dialogs (see hooks/useDialogFocusTrap). Implemented here rather
  // than per call site so all ~24 `Modal` users get it.
  // Escape remains on Modal's bubble-phase listener so a nested layer can
  // consume it with preventDefault before the outer dialog decides to close.
  useDialogFocusTrap(dialogRef, dismiss, { handleEscape: false })

  // Keyboard isolation for the whole dialog, the header X button included. The
  // page's global shortcuts bind bubble-phase `document` keydown
  // (useKeyboardShortcuts), and some chords deliberately fire while an input
  // has focus (the Ctrl+digit session jumps, the Settings chord) — unguarded,
  // one of those typed into a part-filled form navigates away and unmounts the
  // dialog with the draft still in it. Modal portals its panel to
  // document.body, but portal events still propagate through the React tree,
  // so this one handler on the PANEL (not the backdrop) sees every keystroke
  // inside the dialog and stops it before the page's document-level listener.
  //
  // Escape is excepted, and the exception is load-bearing: Modal's own
  // dismissal is a bubble-phase `window` listener (see the `open` effect
  // below), and stopPropagation() on the synthetic event also stops the native
  // event from travelling on to `window` — a blanket stop would break
  // dismissal for every consumer. (The Radix `ui/dialog` family CAN stop
  // everything because its dismissal is capture-phase; see
  // DialogKeyboardIsolation.test.tsx.) Modal's own Escape/Tab handling is
  // otherwise capture-phase (`useDialogFocusTrap` on window), so the trap
  // survives the stop.
  //
  // One exception to the exception: an Escape the IME owns is cancelling a
  // candidate list, not the dialog — the latch claims it (consuming the native
  // event) so it never reaches the window listener and cannot discard a
  // part-composed draft. The latch is document-tracked because the composing
  // input can be anywhere inside the dialog; ModalDialog is mounted only while
  // open, which is the lifecycle the tracking keys on.
  const imeLatch = useDocumentImeLatch()
  const isolateKeys = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      // `claimSyntheticKey` owns both halves of a decline — it consumes the
      // NATIVE event (so the Escape never reaches Modal's own window
      // listener and cannot discard a part-composed draft) and React's own
      // propagation flag, which React walks when dispatching to component
      // ancestors. An Escape it ACCEPTS is deliberately left to propagate:
      // that is the exception documented above, and Modal's dismissal
      // depends on it.
      imeLatch.claimSyntheticKey(e)
      return
    }
    e.stopPropagation()
  }

  // When layoutId is provided, use layout animation (card morph) — no initial/animate/exit needed.
  // Otherwise, use scale+opacity entrance.
  const motionProps = layoutId
    ? { layoutId, transition: SPRING }
    : {
        initial: { scale: 0.95, opacity: 0 } as const,
        animate: { scale: 1, opacity: 1 } as const,
        exit: { scale: 0.95, opacity: 0 } as const,
        transition: SPRING,
      }

  return (
    <motion.div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      // An explicit name wins; otherwise the dialog is named by its own title.
      // `aria-labelledby` is dropped in that case so the two can't disagree.
      aria-label={ariaLabel}
      aria-labelledby={ariaLabel ? undefined : titleId}
      tabIndex={-1}
      onKeyDown={isolateKeys}
      {...motionProps}
      className="bg-card border border-border rounded-xl shadow-2xl w-full flex flex-col pointer-events-auto overflow-hidden outline-none"
      style={{ maxWidth, height, maxHeight: '90vh' }}
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-3 px-5 h-12 shrink-0 border-b border-border">
        <span id={titleId} className="text-base font-semibold text-text-strong truncate">{title}</span>
        <div className="flex items-center gap-1.5 shrink-0">
          {headerActions}
          <button aria-label={i18nT('components.modal.close')} className="p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer" onClick={dismiss}><X size={16} /></button>
        </div>
      </div>
      {/* Body — skipped entirely when the caller renders nothing, so a
          footer-only dialog (e.g. the shared confirm with no detail line)
          doesn't show an empty padded band between header and footer. */}
      {children != null && children !== false && children !== '' && (
        <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden px-5 py-4">
          {children}
        </div>
      )}
      {/* Footer */}
      {footer && (
        <div className="shrink-0 px-5 py-3 border-t border-border flex items-center justify-end gap-2">
          {footer}
        </div>
      )}
    </motion.div>
  )
}

export default function Modal({ open, onClose, maxWidth = 640, guardAccidentalDismiss = false, ...rest }: ModalProps) {
  /** Backdrop + Escape only. Suppressed while the caller guards unsaved input. */
  const softDismiss = useCallback(() => {
    if (!guardAccidentalDismiss) onClose()
  }, [guardAccidentalDismiss, onClose])

  useEffect(() => {
    if (!open) return
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    // Skip an Escape a nested layer already consumed. Overlays that render above
    // this modal call preventDefault on their own Escape handling.
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) softDismiss()
    }
    window.addEventListener('keydown', handler)
    return () => {
      document.body.style.overflow = prevOverflow
      window.removeEventListener('keydown', handler)
    }
  }, [open, softDismiss])

  // Portal to document.body: fixed positioning does not escape an ancestor's
  // clip-path, transform, or filter, and modals can mount inside those shells.
  return createPortal(
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            className="fixed inset-0 bg-bg/60 backdrop-blur-md z-[100]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={softDismiss}
          />
          <div className="fixed inset-0 z-[101] flex items-center justify-center p-8 pointer-events-none">
            <ModalDialog onClose={onClose} maxWidth={maxWidth} {...rest} />
          </div>
        </>
      )}
    </AnimatePresence>,
    document.body
  )
}
