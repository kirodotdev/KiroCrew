import { useLayoutEffect, useRef } from 'react'
import { Loader2, Pause, Play, RotateCcw, Square, Volume2 } from 'lucide-react'

import { i18nT } from '../i18n/t'
import { focusComposer } from '../pages/chat/composerFocus'
import { useAppSelector } from '../store'
import { Glass } from './Glass'
import { Btn } from './ui'

/** Shows above the composer while a reply is being prepared or read aloud, with Pause / Resume and Stop on the first row and Read again on a second row. */
export default function VoicePlaybackBar() {
  const playing = useAppSelector(s => s.chat.voicePlaying)
  const preparing = useAppSelector(s => s.chat.voicePreparing)
  const paused = useAppSelector(s => s.chat.voicePaused)
  if (!playing && !preparing) return null
  return <Bar playing={playing} paused={paused} />
}

function Bar({ playing, paused }: { playing: boolean; paused: boolean }) {
  const rootRef = useRef<HTMLDivElement>(null)
  // Layout cleanup runs while the bar is still in the DOM, so it can tell whether
  // the bar held focus (Stop pressed, or the reading ended on the button).
  useLayoutEffect(() => () => {
    if (rootRef.current?.contains(document.activeElement)) focusComposer()
  }, [])
  return (
    <div ref={rootRef} className="@container pt-1.5" data-testid="voice-playback-bar">
      {/* The dock's glass at the `thick` step, like the progress bars beside it
          (components/Glass.tsx): a bare accent tint is translucent, so the
          transcript scrolling under the bar showed through its label. */}
      <Glass variant="chip" thickness="thick" radius={8} className="glass-accent flex flex-col text-[13px] text-text">
        <div className="flex items-center gap-2 px-3 py-1">
          {paused && <Pause className="lucide-inline shrink-0 text-accent" aria-hidden="true" />}
          {!paused && playing && <Volume2 className="lucide-inline shrink-0 text-accent" aria-hidden="true" />}
          {!paused && !playing && <Loader2 className="lucide-inline shrink-0 text-accent animate-spin motion-reduce:animate-none" aria-hidden="true" />}
          <span role="status" className="min-w-0 flex-1 truncate">
            {i18nT(paused
              ? 'components.voicePlaybackBar.paused'
              : playing ? 'components.voicePlaybackBar.playing' : 'components.voicePlaybackBar.preparing')}
          </span>
          <Btn
            type="button"
            onClick={() => window.dispatchEvent(new Event(paused ? 'voice-resume' : 'voice-pause'))}
            className="shrink-0 bg-bg px-2 py-0.5 [@media(hover:none)]:min-h-10"
            data-testid="voice-playback-pause"
          >
            {paused
              ? <Play className="lucide-inline shrink-0 fill-current" aria-hidden="true" />
              : <Pause className="lucide-inline shrink-0 fill-current" aria-hidden="true" />}
            <span className="@max-[680px]:sr-only">
              {i18nT(paused ? 'components.voicePlaybackBar.resume' : 'components.voicePlaybackBar.pause')}
            </span>
          </Btn>
          <Btn
            type="button"
            onClick={() => window.dispatchEvent(new Event('voice-stop'))}
            className="shrink-0 bg-bg px-2 py-0.5 [@media(hover:none)]:min-h-10"
            data-testid="voice-playback-stop"
          >
            <Square className="lucide-inline shrink-0 fill-current" aria-hidden="true" />
            <span className="@max-[680px]:sr-only">{i18nT('components.voicePlaybackBar.stop')}</span>
          </Btn>
        </div>
        <div className="flex items-center justify-end border-t border-border px-3 py-1">
          <Btn
            type="button"
            onClick={() => window.dispatchEvent(new Event('voice-restart'))}
            aria-label={i18nT('components.voicePlaybackBar.restartFromStart')}
            title={i18nT('components.voicePlaybackBar.restartFromStart')}
            className="shrink-0 bg-bg px-2 py-0.5 [@media(hover:none)]:min-h-10"
            data-testid="voice-playback-restart"
          >
            <RotateCcw className="lucide-inline shrink-0" aria-hidden="true" />
            <span className="max-[360px]:sr-only">{i18nT('components.voicePlaybackBar.restart')}</span>
          </Btn>
        </div>
      </Glass>
    </div>
  )
}
