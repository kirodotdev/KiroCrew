import { Fragment, type ReactNode } from 'react'

interface ComposerFocusOrderProps {
  topDown: boolean
  transcript: ReactNode
  dock: ReactNode
}

/**
 * Puts the composer dock first for top-down keyboard navigation. Stable keyed
 * slots move the existing subtrees instead of remounting either one when the
 * layout setting changes. The top-down dock slot is a zero-height flex item
 * because its child is absolute. `order-last` gives that child the old
 * post-transcript paint order without creating a stacking context.
 */
export default function ComposerFocusOrder({ topDown, transcript, dock }: ComposerFocusOrderProps) {
  const transcriptSlot = <Fragment key="transcript">{transcript}</Fragment>
  const dockSlot = (
    <div key="dock" className={topDown ? 'order-last' : 'contents'} data-composer-focus-order-slot="">
      {dock}
    </div>
  )
  return <>{topDown ? [dockSlot, transcriptSlot] : [transcriptSlot, dockSlot]}</>
}
