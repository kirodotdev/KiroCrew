import { useEffect, useState } from 'react'
import { loadChatConfig } from '../chat/ChatSettings'

/** Settings → Chat → Sessions → Quick reply card on hover, read live through
 *  the same `mc-config-changed` event every chat setting broadcasts. Off means
 *  `ChatSidebar` mounts no `NeedsMeHoverLayer`, so neither hover nor → opens a
 *  card; the rows themselves do not read it. */
export function useNeedsMeHoverCardSetting(): boolean {
  const [on, setOn] = useState(() => loadChatConfig().needsMeHoverCard)
  useEffect(() => {
    const onChange = () => setOn(loadChatConfig().needsMeHoverCard)
    window.addEventListener('mc-config-changed', onChange)
    return () => window.removeEventListener('mc-config-changed', onChange)
  }, [])
  return on
}
