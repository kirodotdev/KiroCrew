import { Mic, MicOff } from 'lucide-react'

import { useActionShortcutHint } from '../hooks/useActionShortcutHint'
import { IS_MAC } from '../hooks/useKeyboardShortcuts'
import { i18nT } from '../i18n/t'

/**
 * Mute toggle for a live dictation, shared by the dictation panel and the
 * status bar. The microphone stays open while muted; the recording keeps its
 * place and its timer, and nothing said is sent until the user unmutes.
 *
 * The tooltip names the chord only while it is still the factory default,
 * because a catalog string can spell only that one. The live chord reaches
 * assistive tech through `aria-keyshortcuts` either way.
 */
export default function VoiceMuteButton({ muted, onToggle, className = '' }: {
  muted: boolean
  onToggle: () => void
  className?: string
}) {
  const hint = useActionShortcutHint('toggle-dictation-mute')
  const label = i18nT(muted ? 'components.voiceMuteButton.unmute' : 'components.voiceMuteButton.mute')
  const chordLine = hint?.isFactory
    ? i18nT(IS_MAC ? 'components.voiceMuteButton.shortcut_hint_mac' : 'components.voiceMuteButton.shortcut_hint')
    : null
  return (
    <button
      type="button"
      data-testid="voice-mute-toggle"
      onClick={onToggle}
      aria-pressed={muted}
      aria-label={label}
      aria-keyshortcuts={hint?.ariaKeyshortcuts}
      title={chordLine ? `${label}\n${chordLine}` : label}
      className={`inline-flex h-5 w-5 shrink-0 items-center justify-center rounded border-none bg-transparent cursor-pointer transition-colors ${className}`}
    >
      {muted ? <MicOff size={13} aria-hidden="true" /> : <Mic size={13} aria-hidden="true" />}
    </button>
  )
}
