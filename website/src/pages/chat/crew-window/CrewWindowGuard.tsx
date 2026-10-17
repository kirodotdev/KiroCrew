/** The crew window's isolation while its chunk loads.
 *
 *  The window is lazy, but its cover is drawn at once. Until the window mounts,
 *  this holds the same two guards it holds: the window counts as shown (so the
 *  approval shortcuts leave the covered local pane alone), and the cover's
 *  siblings are inert (so nothing behind it can be focused or pressed). */
import { useEffect, useRef } from 'react'
import { closeCrewWindow, coverSiblings, markCrewWindowShown } from './crewWindowStore'

export default function CrewWindowGuard({ onClose = closeCrewWindow }: { onClose?: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => markCrewWindowShown(onClose), [onClose])
  useEffect(() => {
    const cover = ref.current?.closest('[data-crew-cover]')
    return cover ? coverSiblings(cover) : undefined
  }, [])
  return <div ref={ref} data-testid="crew-window-loading" />
}
