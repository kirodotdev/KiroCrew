import { copyToClipboard } from './clipboard'

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
): string {
  const basePath = '/chat'
  const slug = title && title !== slotKey ? toSlug(title) : ''

  const params = new URLSearchParams()
  params.set('sid', slotKey)
  if (messageTs) params.set('msg', messageTs)

  const path = `${basePath}${slug ? '/' + slug : ''}`
  return `${window.location.origin}${path}?${params}`
}

export function copySessionLink(
  slotKey: string,
  title?: string,
  messageTs?: string,
  mode?: string,
): Promise<void> {
  return copyToClipboard(buildShareableUrl(slotKey, title, messageTs, mode))
}
