/**
 * The chat settings as LIVE state for a long-lived mount.
 *
 * A one-shot `loadChatConfig()` at mount goes stale the moment the user changes
 * a setting in another tab or on the settings page; this reloads on window
 * focus and on the settings page's `mc-config-changed` event, and keeps
 * referential identity when nothing changed so dependents do not re-render.
 * One implementation for every composer host (ChatPage, ChatPane, ChatEmbed)
 * -- three copies of the same listener block are how the hosts drift apart.
 * Local settings only: no fetch, so an app-sdk embed can use it without
 * touching the dashboard client.
 */
import { useEffect, useState } from 'react'
import { loadChatConfig, type ChatConfig } from '../pages/chat/ChatSettings'

export function useChatConfig(): ChatConfig {
  const [chatConfig, setChatConfig] = useState<ChatConfig>(loadChatConfig)
  useEffect(() => {
    const reload = () => { const next = loadChatConfig(); setChatConfig(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next) }
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])
  return chatConfig
}
