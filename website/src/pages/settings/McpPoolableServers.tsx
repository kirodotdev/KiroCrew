import { useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { SettingsSection, SettingsCard } from '../../components/settings'
import { Toggle } from '../../components/ui'
import { api, type McpPoolableServer } from '../../api/client'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
type GatewayStatus = { enabled: boolean; running: boolean; ping_ok: boolean }

/**
 * Whether a server's poolable toggle is read-only (cannot be flipped here).
 * Locked when the server can never be pooled (denylisted or non-stdio/HTTP) or
 * when it's poolable solely via the agent-JSON escape hatch (poolable:true) and
 * not via the dashboard-managed allowlist — that flag lives outside this UI.
 */
export function poolableRowLocked(srv: McpPoolableServer): boolean {
  return srv.denylisted || srv.transport !== 'stdio' || (srv.entry_poolable && !srv.in_allowlist)
}

function Chip({ kind, children }: { kind: 'blocked' | 'http'; children: ReactNode }) {
  const cls =
    kind === 'blocked'
      ? 'border border-border text-danger'
      : 'border border-border text-muted'
  return <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${cls}`}>{children}</span>
}

/**
 * Per-server poolability management.
 *
 * Lists every MCP server across the user's agent configs and lets them opt each
 * one into the shared gateway's backend pool. Pooling is safe-by-default — a
 * server runs per-session unless it's marked poolable here (or its agent JSON
 * sets poolable:true). The toggle writes the central allowlist
 * (config mcp_gateway.poolable_servers) and re-applies in-process.
 */
export function McpPoolableServers() {
  const qc = useQueryClient()
  const statusQ = useQuery<GatewayStatus>({ queryKey: ['mcpGatewayStatus'], queryFn: () => api.mcpGatewayStatus() })
  const serversQ = useQuery<{ servers: McpPoolableServer[] }>({
    queryKey: ['mcpGatewayServers'],
    queryFn: () => api.mcpGatewayServers(),
  })

  const gatewayEnabled = statusQ.data?.enabled ?? false
  const [pending, setPending] = useState<Set<string>>(new Set())
  const [error, setError] = useState<string | null>(null)

  const servers = serversQ.data?.servers ?? []

  const toggle = async (srv: McpPoolableServer, next: boolean) => {
    setError(null)
    setPending(s => new Set(s).add(srv.name))
    try {
      await api.mcpGatewaySetPoolable(srv.name, next)
      // Refresh the table and the Overview pool metrics so both reflect the change.
      qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
      qc.invalidateQueries({ queryKey: ['mcpGatewayMetrics'] })
    } catch {
      setError(i18nT('pages.settings.mcpPoolableServers.could_not_update_try_again', { name: srv.name }))
    } finally {
      // Clear only this server's flag — concurrent toggles each track their own.
      setPending(s => { const s2 = new Set(s); s2.delete(srv.name); return s2 })
    }
  }

  return (
    <SettingsSection title={i18nT('pages.settings.mcpPoolableServers.poolable_mcp_servers')}>
      <SettingsCard>
        {!gatewayEnabled && (
          <div className="text-[12px] text-muted mb-2">
            {i18nT('pages.settings.mcpPoolableServers.pooling_takes_effect_when_the_shared_mcp_gateway')}
          </div>
        )}

        {serversQ.isLoading ? (
          <div className="flex items-center gap-2 text-[13px] text-muted py-2">
            <Loader2 size={14} className="animate-spin" /> {i18nT('pages.settings.mcpPoolableServers.loading_mcp_servers')}
          </div>
        ) : serversQ.isError ? (
          <div className="text-[13px] text-danger py-2">{i18nT('pages.settings.mcpPoolableServers.could_not_load_mcp_servers')}</div>
        ) : servers.length === 0 ? (
          <div className="text-[13px] text-muted py-2">{i18nT('pages.settings.mcpPoolableServers.no_mcp_servers_configured')}</div>
        ) : (
          <div className="flex flex-col">
            {servers.map(srv => {
              const isStdio = srv.transport === 'stdio'
              // Denylisted / HTTP servers can never be pooled — show their state read-only.
              // The entry_poolable escape hatch (poolable:true in agent JSON) is also locked
              // here: it isn't managed by the allowlist, so we don't let the UI flip it off.
              const locked = poolableRowLocked(srv)
              const rowPending = pending.has(srv.name)
              return (
                <div
                  key={srv.name}
                  className="flex items-center justify-between py-2 border-b border-border last:border-0"
                >
                  <div className="flex-1 min-w-0 mr-4">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-[13px] font-semibold text-text">{srv.name}</span>
                      {srv.denylisted && <Chip kind="blocked">{i18nT('pages.settings.mcpPoolableServers.blocked')}</Chip>}
                      {!isStdio && <Chip kind="http">{srv.transport} {i18nT('pages.settings.mcpPoolableServers.shared')}</Chip>}
                      {srv.entry_poolable && !srv.in_allowlist && <Chip kind="http">{i18nT('pages.settings.mcpPoolableServers.poolable_true')}</Chip>}
                    </div>
                    {srv.agents.length > 0 && (
                      <div className="text-[12px] text-muted mt-0.5 truncate">
                        {i18nT('pages.settings.mcpPoolableServers.used_by')} {srv.agents.join(', ')}
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {rowPending && <Loader2 size={14} className="animate-spin text-accent" />}
                    <Toggle
                      checked={srv.poolable}
                      onChange={next => toggle(srv, next)}
                      disabled={locked || rowPending}
                      label={i18nT('pages.settings.mcpPoolableServers.pool', { name: srv.name })}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        )}

        <ErrorNotice message={error} className="mt-2" askAgent />
      </SettingsCard>
    </SettingsSection>
  )
}
