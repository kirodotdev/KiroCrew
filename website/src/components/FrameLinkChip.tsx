import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ExternalLink } from 'lucide-react'

import { Btn } from './ui'
import { OverlayPortal, useCloseWhenCrewHidden } from './overlayRoot'
import { i18nT } from '../i18n/t'

/** The chip's place: the asking frame's top right corner, kept on screen. */
function placeOf(frame: HTMLIFrameElement): { top: number; right: number } {
  const rect = frame.getBoundingClientRect()
  const top = Math.max(8, Math.min(rect.top + 8, window.innerHeight - 48))
  const right = Math.max(8, Math.min(window.innerWidth - rect.right + 8, window.innerWidth - 120))
  return { top, right }
}

/**
 * "Open <host>": the one way out for a link whose click the dashboard cannot tie
 * to the frame that asks for it (see `useFrameLinkBridge`).
 *
 * A click on this button is the person's own click in the dashboard, so the open
 * it makes borrows nobody's. The full URL is the title, so it shows on hover, as
 * on the link itself. The button takes focus when it appears, so a keyboard user
 * presses Enter again to open. It has no timer, so a slow reader does not lose
 * it: it stays until Escape, any other click or key, a scroll or a resize (it is
 * placed by the frame, so it cannot follow a scroll), or its Crew being hidden.
 */
export function FrameLinkChip({ url, frame, onOpen, onDismiss }: {
  url: string
  frame: HTMLIFrameElement
  onOpen: () => void
  onDismiss: () => void
}) {
  const ref = useRef<HTMLButtonElement | null>(null)
  const [place, setPlace] = useState(() => placeOf(frame))
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss
  // A switch to a remote Crew hides this layer but not its window listeners, and a
  // link asked for on this Crew must not open from there: the button goes.
  useCloseWhenCrewHidden(true, onDismiss)
  useLayoutEffect(() => { setPlace(placeOf(frame)) }, [frame, url])
  useEffect(() => {
    ref.current?.focus({ preventScroll: true })
    const dismiss = () => dismissRef.current()
    const elsewhere = (e: Event) => {
      if (!(e.target instanceof Node) || !ref.current?.contains(e.target)) dismiss()
    }
    const opts = { capture: true, passive: true } as const
    window.addEventListener('pointerdown', elsewhere, opts)
    window.addEventListener('keydown', elsewhere, opts)
    window.addEventListener('scroll', dismiss, opts)
    window.addEventListener('resize', dismiss, opts)
    return () => {
      window.removeEventListener('pointerdown', elsewhere, opts)
      window.removeEventListener('keydown', elsewhere, opts)
      window.removeEventListener('scroll', dismiss, opts)
      window.removeEventListener('resize', dismiss, opts)
    }
  }, [url])
  const host = new URL(url).host
  return (
    <OverlayPortal>
      <Btn
        ref={ref}
        type="button"
        primary
        data-frame-link-chip=""
        title={url}
        className="fixed z-[10000] max-w-[min(32rem,calc(100vw-16px))] shadow-lg"
        style={{ top: place.top, right: place.right }}
        onClick={onOpen}
        onKeyDown={(e) => { if (e.key === 'Escape') onDismiss() }}
      >
        <ExternalLink size={14} aria-hidden="true" />
        <span className="truncate">{i18nT('hooks.frameLinkBridge.openHost', { host })}</span>
      </Btn>
    </OverlayPortal>
  )
}
