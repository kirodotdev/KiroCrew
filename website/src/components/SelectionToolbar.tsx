import { useState, useEffect, useLayoutEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { MessageSquareQuote, MessageCircleQuestion, Copy, Check } from 'lucide-react'
import { copyToClipboard } from '../utils/clipboard'
import { isTouchDevice } from '../utils/isTouchDevice'

export interface SelectionAction {
  id: string
  icon: React.ReactNode
  label: string
  /** Called with selected text and the bounding rect of the selection */
  onClick: (text: string, rect: DOMRect) => void
}

interface SelectionToolbarProps {
  /** Container element to listen for text selection within */
  containerRef: React.RefObject<HTMLElement | null>
  /** Actions to show in the toolbar */
  actions: SelectionAction[]
  /** External trigger (e.g. from Monaco) — shows toolbar at given position with given text */
  externalSelection?: { text: string; x: number; y: number } | null
}

/** Generic floating toolbar that appears when user selects text within a container.
 *  Extensible — pass any actions (quote, copy, etc.) via the `actions` prop. */
export default function SelectionToolbar({ containerRef, actions, externalSelection }: SelectionToolbarProps) {
  const [visible, setVisible] = useState(false)
  const [pos, setPos] = useState({ x: 0, y: 0 })
  // Clamped top-left, computed after measuring the toolbar so it never clips
  // the viewport edges. The layout effect below corrects this before paint,
  // and framer-motion's `initial opacity: 0` hides the mount frame, so there's
  // no visible jump from the pre-measure value.
  const [clampedPos, setClampedPos] = useState({ x: 0, y: 0 })
  // Mirrors clampedPos so the layout effect can compare against the last value
  // without listing the effect's own output in its dependency array (which
  // would fire the effect a second, redundant time on every reposition).
  const clampedRef = useRef({ x: 0, y: 0 })
  const [copiedId, setCopiedId] = useState<string | null>(null)
  // Tracks the "copied!" reset timer so it can be cancelled on unmount — a late
  // setCopiedId firing after the host/jsdom is torn down would touch `window`
  // via React DOM and throw (an uncaught post-teardown ReferenceError).
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const selectedTextRef = useRef('')
  const toolbarRef = useRef<HTMLDivElement>(null)
  const sourceRef = useRef<'dom' | 'external' | null>(null)

  const selectionRectRef = useRef<DOMRect | null>(null)

  const lastMouseRef = useRef({ x: 0, y: 0 })
  const triggeredByMouseRef = useRef(false)

  const checkSelection = useCallback(() => {
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || !sel.toString().trim()) {
      // Only dismiss if toolbar was shown by DOM selection (not external/Monaco)
      if (sourceRef.current === 'dom') setVisible(false)
      return
    }

    const container = containerRef.current
    if (!container) { setVisible(false); return }

    // Ensure selection is within our container
    const range = sel.getRangeAt(0)
    if (!container.contains(range.commonAncestorContainer)) {
      setVisible(false)
      return
    }

    const text = sel.toString().trim()
    if (!text) { setVisible(false); return }

    selectedTextRef.current = text

    const rect = range.getBoundingClientRect()
    selectionRectRef.current = rect
    const x = triggeredByMouseRef.current
      ? lastMouseRef.current.x
      : rect.left + rect.width / 2
    const y = triggeredByMouseRef.current
      ? lastMouseRef.current.y + 8
      : rect.bottom + 8
    setPos({ x, y })
    sourceRef.current = 'dom'
    setVisible(true)
  }, [containerRef])

  // After the toolbar mounts/repositions, measure it and clamp its position so
  // it stays fully inside the viewport. We position by the top-left (left/top)
  // and deliberately do NOT use a CSS translate to center it: this is a
  // framer-motion element, and framer-motion owns the `transform` property for
  // its mount animation (scale/y) — it silently drops any `translate(-50%)` we
  // set, which left the toolbar's left edge (not its center) at the anchor and
  // clipped it by half its width near the right edge (the reported bug).
  // `pos.x` is the desired horizontal center, so convert to a left edge
  // (`pos.x - w/2`) and clamp into [margin, viewportWidth - w - margin].
  // `offsetWidth/Height` report the layout footprint independent of the in-flight
  // scale animation, so the clamp uses the toolbar's true size. Runs in a layout
  // effect so the corrected position commits before paint — no visible jump.
  useLayoutEffect(() => {
    if (!visible) return
    const el = toolbarRef.current
    if (!el) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    const margin = 8
    const vw = window.innerWidth
    const vh = window.innerHeight
    const left = Math.max(margin, Math.min(pos.x - w / 2, vw - w - margin))
    // Flip above the anchor when it would overflow the bottom edge.
    const top = pos.y + h + margin > vh ? Math.max(margin, pos.y - h - margin) : pos.y
    // Compare against the ref (not state) so clampedPos stays out of the deps —
    // the effect runs once per pos change instead of twice.
    if (left !== clampedRef.current.x || top !== clampedRef.current.y) {
      clampedRef.current = { x: left, y: top }
      setClampedPos({ x: left, y: top })
    }
  }, [visible, pos])

  // External trigger (Monaco selections that don't use window.getSelection)
  useEffect(() => {
    if (externalSelection) {
      selectedTextRef.current = externalSelection.text
      selectionRectRef.current = new DOMRect(externalSelection.x, externalSelection.y, 0, 0)
      setPos({ x: externalSelection.x, y: externalSelection.y + 8 })
      sourceRef.current = 'external'
      setVisible(true)
    }
  }, [externalSelection])

  useEffect(() => {
    const onMouseUp = (e: MouseEvent) => {
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      triggeredByMouseRef.current = true
      lastMouseRef.current = { x: e.clientX, y: e.clientY }
      // Small delay to let selection finalize
      setTimeout(checkSelection, 50)
    }

    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setVisible(false); return }
      // Check selection on Shift+Arrow keys (keyboard selection)
      if (e.shiftKey) {
        triggeredByMouseRef.current = false
        setTimeout(checkSelection, 50)
      }
    }

    const onMouseDown = (e: MouseEvent) => {
      // Don't dismiss if clicking inside the toolbar
      if (toolbarRef.current && toolbarRef.current.contains(e.target as Node)) return
      // Clicking inside the container clears the selection (cursor reposition) —
      // dismiss after a tick so the new (empty) selection state is readable.
      if (containerRef.current && containerRef.current.contains(e.target as Node)) {
        setTimeout(() => { if (!window.getSelection()?.toString().trim()) setVisible(false) }, 0)
        return
      }
      setVisible(false)
    }

    // Touch devices never fire `mouseup` for text selection — the selection is
    // made by long-press then adjusted with drag handles, so the mouse-based
    // triggers above never run and the toolbar never appears. `selectionchange`
    // is the reliable cross-mobile signal: it fires as the selection grows and
    // again each time a handle settles. Debounce so the toolbar only appears
    // once the user stops adjusting (avoids flicker mid-drag), and gate to touch
    // so desktop drag-select — which already works via `mouseup` and would show
    // the toolbar prematurely mid-drag under this path — is left unchanged.
    let selectionChangeTimer: ReturnType<typeof setTimeout> | null = null
    const onSelectionChange = () => {
      if (!isTouchDevice()) return
      if (selectionChangeTimer) clearTimeout(selectionChangeTimer)
      // No mouse anchor on touch — checkSelection falls back to the selection
      // rect for positioning when triggeredByMouse is false.
      triggeredByMouseRef.current = false
      selectionChangeTimer = setTimeout(checkSelection, 350)
    }

    document.addEventListener('mouseup', onMouseUp)
    document.addEventListener('keyup', onKeyUp)
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('selectionchange', onSelectionChange)
    return () => {
      document.removeEventListener('mouseup', onMouseUp)
      document.removeEventListener('keyup', onKeyUp)
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('selectionchange', onSelectionChange)
      if (selectionChangeTimer) clearTimeout(selectionChangeTimer)
    }
    // `containerRef` is a stable RefObject (its identity never changes across
    // renders), so listing it does not re-run the effect; it satisfies the
    // linter without changing the listener lifecycle.
  }, [checkSelection, containerRef])

  const handleAction = useCallback((action: SelectionAction) => {
    const text = selectedTextRef.current
    if (!text) return
    const rect = selectionRectRef.current || new DOMRect(0, 0, 0, 0)
    action.onClick(text, rect)
    if (action.id === 'copy') {
      setCopiedId('copy')
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
      copyTimerRef.current = setTimeout(() => {
        copyTimerRef.current = null
        setCopiedId(null)
      }, 1500)
    } else {
      setVisible(false)
      window.getSelection()?.removeAllRanges()
    }
  }, [])

  // Cancel a pending "copied!" reset timer on unmount so it can never fire
  // after the component (or a test's jsdom environment) is torn down.
  useEffect(() => () => {
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
  }, [])

  return createPortal(
    <AnimatePresence>
      {visible && (
        <motion.div
          ref={toolbarRef}
          initial={{ opacity: 0, y: 4, scale: 0.95 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 4, scale: 0.95 }}
          transition={{ duration: 0.15 }}
          className="fixed z-[9999] pointer-events-auto"
          // `clampedPos` is the true top-left after measurement. No CSS
          // translate — framer-motion owns `transform` for its animation and
          // would drop it (see the layout effect above).
          style={{ left: clampedPos.x, top: clampedPos.y }}
        >
          <div className="flex items-center gap-0.5 p-0.5 rounded-lg bg-bg-elevated border border-border shadow-lg">
            {actions.map(action => (
              <button
                key={action.id}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium text-text hover:text-accent hover:bg-bg-hover transition-colors cursor-pointer whitespace-nowrap"
                onMouseDown={e => e.preventDefault()}
                onClick={() => handleAction(action)}
                aria-label={action.label}
                title={action.label}
              >
                {copiedId === action.id ? <Check size={12} className="text-ok" /> : action.icon}
                {action.label}
              </button>
            ))}
          </div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body
  )
}

/** Pre-built actions for common use cases */
export function useSelectionActions(
  onQuote?: (text: string, rect: DOMRect) => void,
  onAsk?: (text: string, rect: DOMRect) => void,
): SelectionAction[] {
  const actions: SelectionAction[] = []

  if (onQuote) {
    actions.push({
      id: 'quote',
      icon: <MessageSquareQuote size={12} />,
      label: 'Quote',
      onClick: onQuote,
    })
  }

  // "Ask" opens the isolated /side conversation seeded with the selection so
  // the user can ask a scoped follow-up WITHOUT polluting the main chat
  // context (unlike Quote, which injects into the main composer).
  if (onAsk) {
    actions.push({
      id: 'ask',
      icon: <MessageCircleQuestion size={12} />,
      label: 'Ask',
      onClick: onAsk,
    })
  }

  actions.push({
    id: 'copy',
    icon: <Copy size={12} />,
    label: 'Copy',
    onClick: (text) => { copyToClipboard(text) },
  })

  return actions
}
