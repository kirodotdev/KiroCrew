import { memo } from 'react'
import { Pin } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { Btn } from '../../components/ui'

const MARK_CLS = 'inline-flex items-center gap-1 mt-1 text-[11px] leading-4 text-accent px-1 py-0.5 rounded'

/**
 * The pinned state a message keeps on screen. The row's Pin / Unpin toggle sits
 * in the hover-revealed action row, so without this a pinned message looks the
 * same as any other once the pointer leaves it.
 *
 * `onShowPins` opens the session's Pins tab, the list of every pinned message.
 * A host that wires none (a chat with no side panel) gets a plain label.
 */
const PinnedMark = memo(function PinnedMark({ onShowPins }: { onShowPins?: () => void }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const body = <><Pin size={11} fill="currentColor" className="shrink-0" aria-hidden="true" /><span>{i18nT('pages.chat.pins.pinned_mark')}</span></>
  // `data-pinned-mark` is the hook chat-transcript.css re-shows while the row stands in for the pinned card.
  if (!onShowPins) return <span className={MARK_CLS} data-testid="pinned-mark" data-pinned-mark="">{body}</span>
  return (
    <Btn
      type="button"
      onClick={onShowPins}
      className={`${MARK_CLS} border-transparent hover:border-transparent hover:bg-accent/10`}
      title={i18nT('pages.chat.pins.show_all_pinned')}
      data-testid="pinned-mark"
      data-pinned-mark=""
    >
      {body}
    </Btn>
  )
})

export default PinnedMark
