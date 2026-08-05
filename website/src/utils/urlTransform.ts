import { defaultUrlTransform } from 'react-markdown'

export const ALLOWED_PROTOCOLS = new Set([
  'vscode:',
  'vscode-insiders:',
])

export function urlTransform(url: string): string {
  try {
    const u = new URL(url)
    if (ALLOWED_PROTOCOLS.has(u.protocol) && u.href.length > u.protocol.length + '//'.length)
      return u.href
  } catch {}
  return defaultUrlTransform(url)
}
