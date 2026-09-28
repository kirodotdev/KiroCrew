import { motion, useReducedMotion } from 'framer-motion'
import { Radar } from 'lucide-react'

interface Props {
  actionRunning: boolean
  className?: string
}

/** A status glyph whose continuous pulse disappears when the OS requests less motion. */
export default function MonitorRadar({ actionRunning, className = '' }: Props) {
  const reducedMotion = useReducedMotion()
  const pulse = actionRunning && !reducedMotion

  return (
    <motion.span
      className="inline-flex shrink-0"
      data-monitor-action-pulse={pulse}
      initial={false}
      animate={pulse ? { opacity: [1, 0.45, 1] } : { opacity: 1 }}
      transition={pulse ? { duration: 1.4, repeat: Infinity, ease: 'easeInOut' } : { duration: 0 }}
    >
      <Radar className={`lucide-inline ${className}`} aria-hidden />
    </motion.span>
  )
}
