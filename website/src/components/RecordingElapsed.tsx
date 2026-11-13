import { useEffect, useRef, useState } from 'react'
import { formatElapsedClock } from '../lib/elapsedClock'

/** How often the reading is re-derived. Finer than a second so the displayed
 *  second flips close to when it really passes; the state only changes once a
 *  second, so this does not re-render four times a second. */
const TICK_MS = 250

interface Props {
  /**
   * Hold the reading where it is. For the window after capture has ended while
   * the surface is still on screen (the streaming drain): the microphone is
   * closed, so a clock that kept running would claim recording time that is
   * not being recorded.
   */
  frozen?: boolean
  className?: string
}

/**
 * How long the current recording has run, counting up from the moment this
 * mounts. Both recording surfaces mount it only while a recording is live, so
 * mount time is the recording's start and no start time has to be threaded in.
 *
 * It only counts up. It knows no limit and shows no countdown: the length a
 * recording may reach belongs to whichever speech provider is transcribing it.
 *
 * Derived from a monotonic clock on each tick, not incremented, so a throttled
 * background tab or a busy main thread makes it late but never wrong.
 *
 * `role="timer"` keeps it out of the surrounding live region's announcements:
 * a reading that changes every second would otherwise be read out every second.
 */
export default function RecordingElapsed({ frozen = false, className }: Props) {
  const startRef = useRef<number | null>(null)
  const [seconds, setSeconds] = useState(0)

  useEffect(() => {
    if (frozen) return
    if (startRef.current === null) startRef.current = performance.now()
    const start = startRef.current
    const tick = () => setSeconds(Math.floor((performance.now() - start) / 1000))
    const id = window.setInterval(tick, TICK_MS)
    return () => window.clearInterval(id)
  }, [frozen])

  return (
    <span
      role="timer"
      data-testid="voice-recording-elapsed"
      className={`shrink-0 tabular-nums ${className ?? ''}`}
    >
      {formatElapsedClock(seconds)}
    </span>
  )
}
