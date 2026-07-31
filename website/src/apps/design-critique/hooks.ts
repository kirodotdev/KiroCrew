import { useState, useEffect, useRef, useCallback } from 'react'
import type { Toast } from './types'

// U18: honour prefers-reduced-motion — no sweep/spin animation for people who ask
// for less. Mirrors the original media-query listener.
export function useReduceMotion(): boolean {
  const [reduce, setReduce] = useState(false)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    setReduce(mq.matches)
    const on = (e: MediaQueryListEvent) => setReduce(e.matches)
    if (mq.addEventListener) mq.addEventListener('change', on)
    else mq.addListener(on)
    return () => {
      if (mq.removeEventListener) mq.removeEventListener('change', on)
      else mq.removeListener(on)
    }
  }, [])
  return reduce
}

// Two-panel shell collapses to stacked layout under 760px.
export function useNarrow(ref: React.RefObject<HTMLElement | null>): boolean {
  const [narrow, setNarrow] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((es) => setNarrow(es[0].contentRect.width < 760))
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return narrow
}

// A tiny page-local toast stack (same shape as DevFleetPage's local toaster).
export function useToasts(): {
  toasts: Toast[]
  notify: (message: string, opts?: { type?: Toast['type'] }) => void
} {
  const [toasts, setToasts] = useState<Toast[]>([])
  const idRef = useRef(0)
  const notify = useCallback((message: string, opts?: { type?: Toast['type'] }) => {
    const id = ++idRef.current
    setToasts(prev => prev.concat([{ id, msg: message, type: opts?.type || 'info' }]))
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 5000)
  }, [])
  return { toasts, notify }
}
