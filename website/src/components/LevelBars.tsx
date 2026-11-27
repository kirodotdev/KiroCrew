import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { AudioSample } from '../hooks/mic'

/* A scrolling live waveform: each tick records the mic level as a new rounded
   bar at the right edge and the history slides left.
   Modelled on the "scrolling" mode of ElevenLabs UI's LiveWaveform (MIT), but
   fed by the composer's existing `AudioSample` instead of opening its own
   microphone, and painted as DOM pills so the bars take the theme colour. */

const INITIAL_BARS = 64
const BAR_WIDTH_PX = 3
const BAR_GAP_PX = 1
const FLOOR = 0.08
const GAIN = 3
const TICK_MS = 40

/** `sampleRef` is read inside the frame loop, so audio drives the bars at frame
 *  rate without a React render. Colour is `currentColor`. */
export default function LevelBars({ sampleRef }: { sampleRef: { current: AudioSample } }) {
  const rootRef = useRef<HTMLDivElement>(null)
  const [barCount, setBarCount] = useState(INITIAL_BARS)
  const historyRef = useRef<number[]>([])

  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root) return
    const measure = (width: number) => {
      if (width <= 0) return
      setBarCount(Math.max(1, Math.floor((width + BAR_GAP_PX) / (BAR_WIDTH_PX + BAR_GAP_PX))))
    }
    measure(root.clientWidth)
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(entries => measure(entries[0]?.contentRect.width ?? root.clientWidth))
    observer.observe(root)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    const bars = Array.from(rootRef.current?.children ?? []) as HTMLElement[]
    // Keep the newest samples across a width change; pad older slots with the floor.
    const kept = historyRef.current.slice(-bars.length)
    const history = [...Array<number>(bars.length - kept.length).fill(FLOOR), ...kept]
    historyRef.current = history
    let raf = 0
    let last = -Infinity
    const tick = (now: number) => {
      if (now - last >= TICK_MS) {
        last = now
        const { level, onset } = sampleRef.current
        history.shift()
        history.push(Math.max(FLOOR, Math.min(1, (level + onset * 0.3) * GAIN)))
        bars.forEach((bar, i) => { bar.style.height = `${history[i] * 100}%` })
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [barCount, sampleRef])
  return (
    <div
      ref={rootRef}
      aria-hidden="true"
      data-testid="level-bars"
      className="flex h-8 items-center justify-end gap-px overflow-hidden text-muted pointer-events-none"
    >
      {Array.from({ length: barCount }, (_, i) => (
        <span
          key={i}
          className="w-[3px] min-h-[3px] shrink-0 rounded-full bg-current"
          style={{ height: `${FLOOR * 100}%` }}
        />
      ))}
    </div>
  )
}
