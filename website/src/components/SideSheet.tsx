/**
 * Right-docked modal sheet.
 *
 * The repo had a centered `Modal`, a resizable inline `DetailPanel`, and a
 * page-specific bottom sheet, but no generic side sheet — this is that gap
 * filled, following the same keyboard contract as the bottom sheet
 * (`useDialogFocusTrap`: focus in on open, focus back out on close, Escape,
 * Tab cycling) so a11y behaviour does not fork per surface.
 *
 * `paused` exists for the nested-dialog case: when a sheet opens a dialog of
 * its own, the sheet must stop trapping Tab and stop answering Escape, or it
 * fights the inner dialog for both. It forwards to the trap hook's `enabled`
 * flag, which gates the key handling WITHOUT disturbing focus — the inner
 * dialog is expected to run its own trap.
 */
import { useRef, type ReactNode } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { X } from 'lucide-react'
import Clickable from './Clickable'
import { IconButton } from './ui'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { i18nT } from '../i18n/t'

export interface SideSheetProps {
  open: boolean
  onClose: () => void
  /** Accessible name for the dialog. */
  label: string
  /** Header content — usually an avatar plus a title block. */
  header: ReactNode
  /** Right-aligned header controls. */
  headerActions?: ReactNode
  /** Sticky footer, typically the save/cancel pair. */
  footer?: ReactNode
  /** Max width in px. The sheet is full-width below that. */
  width?: number
  /** A nested dialog owns the keyboard — stop trapping Tab and ignore Escape. */
  paused?: boolean
  children: ReactNode
}

/** Mounted only while open so the focus-trap hook's lifecycle matches the sheet's. */
function Sheet({ onClose, label, header, headerActions, footer, width, paused, children }: Omit<SideSheetProps, 'open'>) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()

  // The ref stays stable across pausing; only the key handling is switched off,
  // so focus is neither restored nor re-grabbed when the nested dialog opens.
  useDialogFocusTrap(dialogRef, onClose, !paused)

  return (
    <div className="fixed inset-0 z-[90] flex justify-end">
      {/* Backdrop is a SIBLING of the dialog, never a wrapper — wrapping would
          put the dialog's own controls inside a role="button". */}
      <Clickable
        className="absolute inset-0 bg-bg/50 backdrop-blur-sm"
        onClick={onClose}
        aria-label={i18nT('components.sideSheet.close_panel')}
      />
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        initial={reduceMotion ? { opacity: 0 } : { x: '100%' }}
        animate={reduceMotion ? { opacity: 1 } : { x: 0 }}
        exit={reduceMotion ? { opacity: 0 } : { x: '100%' }}
        transition={{ duration: 0.24, ease: 'easeOut' }}
        style={{ maxWidth: width }}
        className="relative flex h-full w-full min-h-0 min-w-0 flex-col overflow-hidden border-l
                   border-border bg-bg shadow-lg outline-none"
        // The page's own keyboard shortcuts must not fire while typing in here.
        onKeyDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-border px-5 py-4">
          {header}
          {headerActions}
          <IconButton
            aria-label={i18nT('components.sideSheet.close_panel')}
            onClick={onClose}
          >
            <X className="lucide-inline" aria-hidden="true" />
          </IconButton>
        </div>
        <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-5 py-5">{children}</div>
        {footer && (
          <div className="flex items-center gap-2 border-t border-border bg-bg-accent px-5 py-3">{footer}</div>
        )}
      </motion.div>
    </div>
  )
}

export default function SideSheet({ open, ...rest }: SideSheetProps) {
  return <AnimatePresence>{open && <Sheet {...rest} />}</AnimatePresence>
}
