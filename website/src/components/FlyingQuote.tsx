import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { motion, useMotionValue, useSpring, useTransform } from 'framer-motion'

interface FlyingQuoteProps {
  /** Bounding rect of the selected text (source) */
  from: DOMRect
  /** Target element to fly toward (the input box) */
  targetRef: React.RefObject<HTMLElement | null>
  /** Text snippet to display in the flying element */
  text: string
  /** Called when animation completes */
  onComplete: () => void
}

/** Safari-download-style "pluck" animation.
 *  Spring on Y with upward initial velocity naturally creates a parabolic arc. */
export default function FlyingQuote({ from, targetRef, text, onComplete }: FlyingQuoteProps) {
  const doneRef = useRef(false)
  const onCompleteRef = useRef(onComplete)
  onCompleteRef.current = onComplete

  const startX = from.left + from.width / 2
  const startY = from.top + from.height / 2

  const rawX = useMotionValue(startX)
  const rawY = useMotionValue(startY)
  const targetYRef = useRef(startY)

  // X: slower spring — horizontal movement lags behind the vertical lift
  const x = useSpring(rawX, { stiffness: 55, damping: 16, mass: 1.2 })
  // Y: spring with upward velocity — overshoots up, creating the parabolic arc
  const y = useSpring(rawY, { stiffness: 70, damping: 16, mass: 1.2, velocity: -3000 })

  // Derive scale/opacity from Y distance to target (ref avoids per-frame reflow)
  const scale = useTransform(y, (v) => {
    const targetY = targetYRef.current
    const totalDist = Math.abs(targetY - startY) || 1
    const progress = Math.max(0, 1 - Math.abs(v - targetY) / totalDist)
    // Accelerating shrink — slow at first, rapid at the end (sucked in)
    const eased = progress * progress * progress
    return 1.05 - eased * 0.85 // 1.05 → 0.2
  })
  const opacity = useTransform(y, (v) => {
    const targetY = targetYRef.current
    const totalDist = Math.abs(targetY - startY) || 1
    const progress = Math.max(0, 1 - Math.abs(v - targetY) / totalDist)
    // Stay fully opaque, snap to 0 right at the end
    return progress > 0.92 ? Math.max(0, 1 - (progress - 0.92) * 12.5) : 1
  })

  useEffect(() => {
    const el = targetRef.current
    if (!el) { onCompleteRef.current(); return }
    // Find the actual textarea inside the wrapper
    const textarea = el.querySelector('textarea')
    const rect = (textarea || el).getBoundingClientRect()
    targetYRef.current = rect.bottom
    // Target the bottom-left corner of the textarea
    rawX.set(rect.left)
    rawY.set(rect.bottom)
  }, [targetRef, rawX, rawY])

  // Detect when spring settles near target
  useEffect(() => {
    const unsub = y.on('change', (v) => {
      if (doneRef.current) return
      const targetY = targetYRef.current
      if (Math.abs(v - targetY) < 3) {
        doneRef.current = true
        setTimeout(() => onCompleteRef.current(), 30)
      }
    })
    return unsub
  }, [y])

  const truncated = text.length > 60 ? text.slice(0, 57) + '…' : text

  return createPortal(
    <motion.div
      style={{
        position: 'fixed',
        left: x,
        top: y,
        x: '-50%',
        y: '-50%',
        scale,
        opacity,
      }}
      className="fixed z-[99999] pointer-events-none max-w-[280px]"
    >
      <div className="px-3 py-2 rounded-lg bg-accent/15 border border-accent/30 backdrop-blur-sm shadow-lg">
        <div className="flex items-start gap-2">
          <div className="w-0.5 h-full min-h-[16px] bg-accent rounded-full shrink-0" />
          <span className="text-[12px] text-text font-mono leading-snug line-clamp-2">{truncated}</span>
        </div>
      </div>
    </motion.div>,
    document.body
  )
}
