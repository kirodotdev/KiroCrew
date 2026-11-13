import { Mic, MicOff } from 'lucide-react'

import { useActionShortcutHint } from '../hooks/useActionShortcutHint'
import { IS_MAC } from '../hooks/useKeyboardShortcuts'
import { i18nT } from '../i18n/t'

/**
 * Mute toggle for a live dictation, shared by the dictation panel and the
 * status bar. The microphone stays open while muted; the recording keeps its
 * place and its timer, and nothing said is sent until the user unmutes.
 *
 * It carries a visible word, not only an icon. The device picker beside it is
 * also a microphone icon, and the composer says "click mic to stop", so a bare
 * icon here reads as the control that ends the recording.
 *
 * The tooltip names the chord only while it is still the factory default,
 * because a catalog string can spell only that one. The live chord reaches
 * assistive tech through `aria-keyshortcuts` either way.
 *
 * The name says what a press will do ("Mute microphone" / "Unmute
 * microphone") and there is no `aria-pressed`. With both, a muted mic is read
 * out as "Unmute microphone, pressed", which says the opposite of the state.
 * The state itself is the strip's "Muted" label, in a polite live region.
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
      aria-label={label}
      aria-keyshortcuts={hint?.ariaKeyshortcuts}
      title={chordLine ? `${label}\n${chordLine}` : label}
      className={`inline-flex h-5 shrink-0 items-center gap-1 rounded border border-current/30 bg-transparent px-1.5 text-[11px] font-medium leading-none cursor-pointer transition-colors ${className}`}
    >
      {muted ? <MicOff size={12} aria-hidden="true" /> : <Mic size={12} aria-hidden="true" />}
      {i18nT(muted ? 'components.voiceMuteButton.unmute_short' : 'components.voiceMuteButton.mute_short')}
    </button>
  )
}
