// Input-level bar for an active recording.
//
// Subscribes to the recording socket's `level` events directly rather than taking
// the level as a prop. Those arrive ~5 Hz, and threading them through
// `useMeetingSession` state would re-render MeetingView and every AgentPanel five
// times a second for a 40-pixel bar. This component is the only thing that
// re-renders.

import { useEffect, useState } from 'react'

/**
 * Map an RMS level in [0, 1] to a bar fill fraction in [0, 1].
 *
 * Speech RMS lives in roughly 0.02-0.2, so a linear bar reads as dead at normal
 * talking volume and the meter stops being evidence that audio is arriving —
 * which is the one job it has. The square root expands that low end (0.02 -> 0.25,
 * 0.2 -> 0.80) while staying monotonic, so a loud passage still visibly differs
 * from a quiet one.
 *
 * Exported for its own test: it is pure, and the component around it is not.
 */
export function meterFill(rms: number): number {
  if (!Number.isFinite(rms) || rms <= 0) return 0
  return Math.min(1, Math.sqrt(rms) * 1.8)
}

interface Props {
  /** `useMeetingRecording`'s `subscribeLevel`. Returns its own unsubscribe. */
  subscribe: (fn: (rms: number) => void) => () => void
}

export default function RecordingMeter({ subscribe }: Props) {
  const [rms, setRms] = useState(0)

  useEffect(() => subscribe(setRms), [subscribe])

  return (
    // Decorative on purpose: `aria-hidden`. Whether a recording is running is
    // already conveyed by the button's own label and pressed state, and a live
    // numeric value here would have a screen reader announcing a changing
    // percentage five times a second with nothing actionable in it.
    <div
      aria-hidden="true"
      className="w-10 h-1.5 rounded-full bg-bg-hover overflow-hidden"
    >
      <div
        className="h-full bg-danger rounded-full origin-left transition-[width] duration-150 ease-out"
        style={{ width: `${meterFill(rms) * 100}%` }}
      />
    </div>
  )
}
