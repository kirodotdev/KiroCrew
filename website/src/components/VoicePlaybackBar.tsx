import { useLayoutEffect, useRef } from 'react'
import { Loader2, Square, Volume2 } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { focusComposer } from '../pages/chat/composerFocus'
import { useAppSelector } from '../store'
import { Btn } from './ui'

/** Shows above the composer while a reply is being prepared or read aloud, with a Stop button. */
export default function VoicePlaybackBar() {
  const playing = useAppSelector(s => s.chat.voicePlaying)
  const preparing = useAppSelector(s => s.chat.voicePreparing)
  if (!playing && !preparing) return null
  return <Bar playing={playing} />
}

function Bar({ playing }: { playing: boolean }) {
  const rootRef = useRef<HTMLDivElement>(null)
  // Layout cleanup runs while the bar is still in the DOM, so it can tell whether
  // the bar held focus (Stop pressed, or the reading ended on the button).
  useLayoutEffect(() => () => {
    if (rootRef.current?.contains(document.activeElement)) focusComposer()
  }, [])
  return (
    <div ref={rootRef} className="pt-1.5" data-testid="voice-playback-bar">
      <div className="flex items-center gap-2 rounded-lg border border-accent/40 bg-accent-subtle px-3 py-1 text-[13px] text-text">
        {playing
          ? <Volume2 className="lucide-inline shrink-0 text-accent" aria-hidden="true" />
          : <Loader2 className="lucide-inline shrink-0 text-accent animate-spin motion-reduce:animate-none" aria-hidden="true" />}
        <span role="status" className="min-w-0 flex-1 truncate">
          {i18nT(playing ? 'components.voicePlaybackBar.playing' : 'components.voicePlaybackBar.preparing')}
        </span>
        <Btn
          type="button"
          onClick={() => window.dispatchEvent(new Event('voice-stop'))}
          className="shrink-0 bg-bg px-2 py-0.5 [@media(hover:none)]:min-h-10"
          data-testid="voice-playback-stop"
        >
          <Square className="lucide-inline shrink-0 fill-current" aria-hidden="true" />
          {i18nT('components.voicePlaybackBar.stop')}
        </Btn>
      </div>
    </div>
  )
}
