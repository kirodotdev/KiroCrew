import { useLayoutEffect, useRef } from 'react'

import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

/**
 * Top-of-transcript marker shown whenever the server reported unloaded history.
 *
 * It signals that older messages exist and gives an explicit control to anyone
 * whose scroll method the automatic trigger cannot observe. The in-flight state is
 * left to the sticky spinner above the transcript, which is the only indicator
 * during a chat switch -- this bar is unmounted then, because its own mount
 * condition is reset while the switch is in progress.
 */
export default function EarlierMessagesBar({ loading, failed, onLoad, onFocusRelease }: {
  loading: boolean
  failed: boolean
  onLoad: () => void
  /** Called on unmount only while this control still holds focus. */
  onFocusRelease?: () => void
}) {
  const btnRef = useRef<HTMLButtonElement | null>(null)
  const releaseRef = useRef(onFocusRelease)
  useLayoutEffect(() => { releaseRef.current = onFocusRelease })
  // The last page unmounts this bar, so whoever just activated it would land on
  // <body>: hand focus to the transcript rather than drop it.
  useLayoutEffect(() => () => {
    const btn = btnRef.current
    if (btn && document.activeElement === btn) releaseRef.current?.()
  }, [])

  const label = failed
    ? i18nT('pages.chat.earlierMessagesBar.error_retry')
    : i18nT('pages.chat.earlierMessagesBar.load_earlier_messages')

  return (
    <div className="flex justify-center px-4 py-2 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <Btn
        ref={btnRef}
        type="button"
        data-testid="load-earlier-messages"
        onClick={() => { if (!loading) onLoad() }}
        // aria-disabled, not disabled: a disabled button drops focus to <body>, and
        // this control exists for the keyboard and AT users that would strand.
        aria-disabled={loading}
        aria-busy={loading}
        className={[
          'text-[13px] leading-5',
          failed ? 'text-danger border-danger/40' : 'text-muted',
          loading ? 'opacity-50' : '',
        ].join(' ')}
      >
        {label}
      </Btn>
    </div>
  )
}
