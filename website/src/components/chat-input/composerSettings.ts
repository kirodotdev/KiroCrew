import { useChatSetting } from '../../hooks/useChatSetting'

/**
 * The composer's Settings -> Chat display values. Each is the host's prop when
 * it passes one and the user's stored choice when it does not, so every host of
 * ChatInput (the Sessions chat, a Bot's chat, a split-view pane, a pop-out crew
 * window, the side panel) follows the same settings and none can drop one by
 * forgetting to thread it. The send key has its own reader,
 * `useComposerSendMode`.
 */
export function useComposerSettings(props: {
  showFullPastes?: boolean
  showContextPct?: boolean
  showContextTokens?: boolean
}) {
  const showFullPastes = useChatSetting('showFullPastes')
  const showContextPct = useChatSetting('showContextPct')
  const showContextTokens = useChatSetting('showContextTokens')
  return {
    showFullPastes: props.showFullPastes ?? showFullPastes,
    showContextPct: props.showContextPct ?? showContextPct,
    showContextTokens: props.showContextTokens ?? showContextTokens,
  }
}
