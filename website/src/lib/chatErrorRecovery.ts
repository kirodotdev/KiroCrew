import type { ChatMessage } from '../types'

/** Keep the persisted diagnostic intact; hide its code only in the error card. */
export function chatErrorDisplayText(content: string, meta: ChatMessage['meta']): string {
  if (meta?.code !== 'memory_unavailable' && meta?.code !== 'materialization_changed') return content
  const prefix = `${meta.code}:`
  return content.startsWith(prefix)
    ? content.slice(prefix.length).trimStart()
    : content
}
