import { useQuery } from '@tanstack/react-query'
import { api, type TunnelStatus as TunnelStatusData } from '../api/client'
import { StatCard } from './ui'

import { i18nT } from '../i18n/t'
/** Human-readable label, color, and tooltip for a tunnel status.
 *  Pure function — unit tested independently of react-query.
 *
 *  Fork note: returns `null` when the status is null/unfetched OR the tunnel
 *  is `disabled`. The caller renders nothing in those cases, so the public
 *  edition (permanently `disabled`) shows zero pixels and there is no em-dash
 *  flash before the first poll resolves. */
export function tunnelDisplay(
  s: TunnelStatusData | null,
): { value: string; colorClass: string; tooltip?: string } | null {
  if (!s) return null
  switch (s.state) {
    case 'connected': {
      const mins = Math.max(0, Math.round((s.uptime || 0) / 60))
      const up = mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}m`
      const tip = s.url ? `${s.url}${s.uptime ? ` · ${i18nT('components.tunnelStatus.up', { time: up })}` : ''}` : i18nT('components.tunnelStatus.tunnel_connected')
      return { value: i18nT('components.tunnelStatus.connected'), colorClass: 'text-accent', tooltip: tip }
    }
    case 'starting':
      return { value: i18nT('components.tunnelStatus.connecting'), colorClass: 'text-warn', tooltip: i18nT('components.tunnelStatus.tunnel_is_starting_up') }
    case 'reconnecting':
      return {
        value: i18nT('components.tunnelStatus.reconnecting'),
        colorClass: 'text-warn',
        tooltip: `${i18nT('components.tunnelStatus.reconnect_attempt', { n: s.reconnect_attempt || 0 })}${s.error ? ` · ${s.error}` : ''}`,
      }
    case 'error':
      return { value: i18nT('components.tunnelStatus.error'), colorClass: 'text-danger', tooltip: s.error || i18nT('components.tunnelStatus.tunnel_error') }
    case 'stopped':
      return { value: i18nT('components.tunnelStatus.stopped'), colorClass: 'text-muted', tooltip: i18nT('components.tunnelStatus.tunnel_stopped') }
    case 'disabled':
    default:
      // Fork adaptation: `disabled` is the permanent public-edition state, so
      // render nothing rather than an "Off" tile — companion users with an
      // active tunnel get the tile; OSS users see zero pixels.
      return null
  }
}

/** Overview stat tile showing tunnel connectivity for mobile dashboard access.
 *  Renders nothing while the status is unfetched or the tunnel is disabled. */
export function TunnelStatus({ delay }: { delay?: number }) {
  const { data = null } = useQuery<TunnelStatusData>({
    queryKey: ['tunnel-status'],
    queryFn: () => api.tunnelStatus(),
    // Poll every 15s while the tunnel can still change state, but stop once it
    // reports `disabled` — the permanent public-edition state. Otherwise every
    // focused Overview tab would fire a request every 15s forever against a
    // handler that only ever returns the same static `disabled` dict, and the
    // tile renders nothing anyway.
    refetchInterval: (query) => (query.state.data?.state === 'disabled' ? false : 15_000),
  })
  const display = tunnelDisplay(data)
  if (!display) return null
  const { value, colorClass, tooltip } = display
  return <StatCard label={i18nT('components.tunnelStatus.tunnel')} value={value} colorClass={colorClass} title={tooltip} delay={delay} />
}
