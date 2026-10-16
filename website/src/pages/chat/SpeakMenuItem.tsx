import { useContext, useSyncExternalStore } from 'react'
import { ReactReduxContext } from 'react-redux'
import { Square, Volume2 } from 'lucide-react'

import { DropdownMenuItem } from '../../components/ui/dropdown-menu'
import { i18nT } from '../../i18n/t'
import type { AppStore } from '../../store'

const subscribeToNothing = () => () => {}

/** Whether a reply is being read aloud right now, or false with no store.
 *
 *  `AssistantMessage` is also rendered by the app SDK's `ChatEmbed`, which
 *  carries no Redux, so this reads the store only when a react-redux Provider
 *  is above it instead of calling `useAppSelector`, which throws without one.
 *  Only an open More menu mounts the item, so only it subscribes. */
function useVoicePlaying(): boolean {
  const store = (useContext(ReactReduxContext)?.store ?? null) as AppStore | null
  return useSyncExternalStore(
    store ? store.subscribe : subscribeToNothing,
    () => (store ? store.getState().chat.voicePlaying : false),
  )
}

/** The More menu's Read aloud item. Picking it while audio plays stops the
 *  reading (`handleSpeak` in `messageActions.ts`), from whichever reply it is
 *  picked on, so while audio plays it is labelled and drawn as Stop. While the
 *  audio is still being prepared a pick starts this reply instead, so it keeps
 *  reading "Read aloud" until playback begins. */
export default function SpeakMenuItem({ onSelect }: { onSelect: () => void }) {
  const playing = useVoicePlaying()
  return (
    <DropdownMenuItem
      className="[@media(hover:none)]:min-h-10"
      data-testid="speak-message"
      data-playing={playing ? 'true' : undefined}
      aria-description={playing ? undefined : i18nT('pages.chat.assistantMessage.speak_message')}
      onSelect={onSelect}
    >
      <span className="flex items-center gap-2">
        {playing
          ? <Square className="lucide-inline shrink-0 fill-current" />
          : <Volume2 className="lucide-inline shrink-0" />}
        <span>{i18nT(playing ? 'pages.chat.assistantMessage.stop_reading' : 'pages.chat.assistantMessage.speak')}</span>
      </span>
    </DropdownMenuItem>
  )
}
