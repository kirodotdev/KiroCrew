import { Lock } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { slotChannelNamespace } from '../utils/channelOrigin'
import { connectionForTransport, isRestricted, useConnections } from '../hooks/useConnections'

/**
 * Row glyph marking a session whose chat connection is bounded by governance.
 *
 * One of the inline session-row glyphs (incognito eye, clean-mode droplet,
 * pinned pin) rather than a badge: the row's job is to be scannable, and the only
 * new fact worth one glyph is that this conversation's caller cannot do
 * everything. The header chip carries the detail.
 *
 * Renders nothing for a dashboard session and nothing for an unconstrained
 * connection — a lock on every channel row would stop meaning anything.
 *
 * Its own component, not a value threaded through the row, so the sidebar does
 * not grow another data dependency: every instance reads the same react-query
 * cache entry, so N rows still cost one request.
 */
export default function SessionConnectionLock({ slotKey }: { slotKey: string }) {
  const { data } = useConnections()
  const transport = slotChannelNamespace(slotKey)
  const connection = connectionForTransport(data, transport)
  if (!transport || !connection || !isRestricted(connection)) return null

  const label = i18nT('components.sessionConnectionLock.tooltip', { id: connection.id })
  return (
    <span className="text-muted shrink-0 inline-flex items-center" title={label} aria-label={label}>
      <Lock size={10} aria-hidden />
    </span>
  )
}
