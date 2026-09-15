import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DragEvent as ReactDragEvent } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import kiroFileChomper from '../assets/kiro-file-chomper.png'
import { i18nT } from '../i18n/t'

type DropHandler = (event: ReactDragEvent) => void
type DropTargetProps = {
  onDragEnter: DropHandler
  onDragOver: DropHandler
  onDragLeave: DropHandler
  onDrop: DropHandler
}

const FILE_COPY_DROP_EFFECT: DataTransfer['dropEffect'] = 'copy'
const BOB_CYCLE_SECONDS = 2.8

function carriesFiles(dataTransfer: DataTransfer): boolean {
  return dataTransfer.types?.includes('Files')
    || Array.from(dataTransfer.items).some((item) => item.kind === 'file')
    || dataTransfer.files.length > 0
}

/**
 * Owns one file drag across an entire chat pane. A depth counter absorbs the
 * enter/leave pairs emitted while the pointer crosses nested chat controls,
 * while the file check leaves the session-grid's internal DnD untouched.
 */
export function useChatFileDrop(
  onDrop: (dataTransfer: DataTransfer) => void,
): { active: boolean; dropTargetProps: DropTargetProps } {
  const [active, setActive] = useState(false)
  const depthRef = useRef(0)
  const suppressedRef = useRef(false)

  const reset = useCallback(() => {
    depthRef.current = 0
    suppressedRef.current = false
    setActive(false)
  }, [])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !active) return
      suppressedRef.current = true
      setActive(false)
    }
    const onWindowBlur = () => reset()
    const onDragEnd = () => reset()
    // A drop outside the pane can end the browser drag without delivering the
    // matching dragleave events. Defer cleanup until React has dispatched any
    // pane-level drop handler registered through its delegated event system.
    const onWindowDrop = () => window.setTimeout(reset, 0)
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('blur', onWindowBlur)
    window.addEventListener('dragend', onDragEnd, true)
    window.addEventListener('drop', onWindowDrop)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('blur', onWindowBlur)
      window.removeEventListener('dragend', onDragEnd, true)
      window.removeEventListener('drop', onWindowDrop)
    }
  }, [active, reset])

  const onDragEnter = useCallback((event: ReactDragEvent) => {
    if (!carriesFiles(event.dataTransfer)) return
    event.preventDefault()
    event.stopPropagation()
    depthRef.current += 1
    if (!suppressedRef.current) setActive(true)
  }, [])

  const onDragOver = useCallback((event: ReactDragEvent) => {
    if (!carriesFiles(event.dataTransfer)) return
    event.preventDefault()
    event.stopPropagation()
    event.dataTransfer.dropEffect = suppressedRef.current ? 'none' : FILE_COPY_DROP_EFFECT
    if (!suppressedRef.current) setActive(true)
  }, [])

  const onDragLeave = useCallback((event: ReactDragEvent) => {
    if (!carriesFiles(event.dataTransfer)) return
    event.preventDefault()
    event.stopPropagation()
    depthRef.current = Math.max(0, depthRef.current - 1)
    if (depthRef.current === 0) reset()
  }, [reset])

  const onDropEvent = useCallback((event: ReactDragEvent) => {
    if (!carriesFiles(event.dataTransfer)) return
    event.preventDefault()
    event.stopPropagation()
    const cancelled = suppressedRef.current
    reset()
    if (!cancelled) onDrop(event.dataTransfer)
  }, [onDrop, reset])

  const dropTargetProps = useMemo(() => ({
    onDragEnter,
    onDragOver,
    onDragLeave,
    onDrop: onDropEvent,
  }), [onDragEnter, onDragLeave, onDragOver, onDropEvent])

  return { active, dropTargetProps }
}

export default function ChatDropOverlay({
  active,
}: {
  active: boolean
}) {
  const reduceMotion = useReducedMotion()

  return (
    <AnimatePresence initial={false}>
      {active && (
        <motion.div
          data-testid="chat-drop-overlay"
          className="pointer-events-none absolute inset-0 z-[60] flex items-center justify-center overflow-hidden bg-bg/95 p-6"
          initial={reduceMotion ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduceMotion ? 0 : 0.14, ease: 'easeOut' }}
          role="status"
          aria-live="polite"
        >
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-2 rounded-xl border-2 border-dashed border-accent/60"
          />
          <motion.div
            className="relative flex flex-col items-center"
            initial={reduceMotion ? false : { opacity: 0, scale: 0.97, y: 4 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 2 }}
            transition={{ duration: reduceMotion ? 0 : 0.14, ease: 'easeOut' }}
          >
            <motion.img
              src={kiroFileChomper}
              alt=""
              draggable={false}
              aria-hidden="true"
              className="h-[140px] w-[200px] select-none object-contain [will-change:transform]"
              animate={reduceMotion ? undefined : { y: [0, -5, 0], rotate: [-1, 1.2, -1] }}
              transition={reduceMotion ? undefined : {
                duration: BOB_CYCLE_SECONDS,
                ease: 'easeInOut',
                repeat: Infinity,
              }}
            />
            <span className="mt-4 text-[13px] font-medium text-text-strong">
              {i18nT('components.chatInput.drop_to_attach')}
            </span>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
