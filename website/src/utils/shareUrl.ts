import { copyToClipboard } from './clipboard'

/** Minimal message shape needed for deep-link resolution (avoids importing ChatMessage). */
export interface ResolvableMessage {
  ts?: string
  meta?: Record<string, unknown>
}

/**
 * Resolve a deep-link target to a message index.
 * Prefers mid-based lookup (stable per-message identity) so same-timestamp
 * messages from the same tick are disambiguated.  Falls back to ts for legacy
 * links that carry no mid param.
 */
export function resolveMsgIndex(
  messages: ResolvableMessage[],
  targetTs: string,
  targetMid?: string | null,
): number {
  if (targetMid) {
    const byMid = messages.findIndex(m => m.meta?.mid === targetMid)
    if (byMid >= 0) return byMid
  }
  return messages.findIndex(m => m.ts === targetTs)
}

export function toSlug(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 80)
    .replace(/-$/, '')
}

export function buildShareableUrl(
  slotKey: string,
  title?: string,
  messageTs?: string,
  _mode?: string,
  mid?: string,
): string {
  const basePath = '/chat'
  const slug = title && title !== slotKey ? toSlug(title) : ''

  const params = new URLSearchParams()
  params.set('sid', slotKey)
  if (messageTs) params.set('msg', messageTs)
  if (mid) params.set('mid', mid)

  const path = `${basePath}${slug ? '/' + slug : ''}`
  return `${window.location.origin}${path}?${params}`
}

export function copySessionLink(
  slotKey: string,
  title?: string,
  messageTs?: string,
  mode?: string,
  mid?: string,
): Promise<boolean> {
  return copyToClipboard(buildShareableUrl(slotKey, title, messageTs, mode, mid))
}
