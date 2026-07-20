import React, { useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X } from 'lucide-react'

interface ModalProps {
  /** Whether the modal is open */
  open: boolean
  /** Called when the modal should close (backdrop click, Escape, or X button) */
  onClose: () => void
  /** Modal title displayed in the header */
  title: React.ReactNode
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
  /** Modal content */
  children: React.ReactNode
}

const SPRING = { type: 'spring' as const, stiffness: 500, damping: 35 }

export default function Modal({ open, onClose, title, footer, headerActions, maxWidth = 640, height, layoutId, children }: ModalProps) {
  const dismiss = useCallback(() => onClose(), [onClose])

  useEffect(() => {
    if (!open) return
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') dismiss() }
    window.addEventListener('keydown', handler)
    return () => {
      document.body.style.overflow = prevOverflow
      window.removeEventListener('keydown', handler)
    }
  }, [open, dismiss])

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
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            className="fixed inset-0 bg-bg/60 backdrop-blur-md z-[100]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={dismiss}
          />
          <div className="fixed inset-0 z-[101] flex items-center justify-center p-8 pointer-events-none">
            <motion.div
              role="dialog"
              aria-modal="true"
              {...motionProps}
              className="bg-card border border-border rounded-xl shadow-2xl w-full flex flex-col pointer-events-auto overflow-hidden"
              style={{ maxWidth, height, maxHeight: '90vh' }}
            >
              {/* Header */}
              <div className="flex items-center justify-between gap-3 px-5 h-12 shrink-0 border-b border-border">
                <span className="text-base font-semibold text-text-strong truncate">{title}</span>
                <div className="flex items-center gap-1.5 shrink-0">
                  {headerActions}
                  <button aria-label="Close" className="p-1.5 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer" onClick={dismiss}><X size={16} /></button>
                </div>
              </div>
              {/* Body */}
              <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden px-5 py-4">
                {children}
              </div>
              {/* Footer */}
              {footer && (
                <div className="shrink-0 px-5 py-3 border-t border-border flex items-center justify-end gap-2">
                  {footer}
                </div>
              )}
            </motion.div>
          </div>
        </>
      )}
    </AnimatePresence>
  )
}
