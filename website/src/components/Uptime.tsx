import { useUptime } from '../hooks/useUptime'

/** The gateway's live uptime as text. Its own component so the 1 Hz tick
 *  repaints this text alone, not the page that shows it. */
export function Uptime() {
  return <>{useUptime()}</>
}
