import { useState, useCallback } from 'react'
import { useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Check, Star } from 'lucide-react'
import { motion } from 'framer-motion'
import { api } from '../api/client'
import { Card, Btn, SearchInput } from './ui'
import InfoTip from './InfoTip'
import McpDetailModal from './McpDetailModal'
import { useProvider } from '../providers'
import type { McpServer } from '../types'

interface RegistryServer {
  id: string; installed: string; title: string; tier: string; description: string
}

/** Extract the first meaningful paragraph from a markdown description. */
function summaryFromDescription(desc: string): string {
  if (!desc) return ''
  let inCodeBlock = false
  for (const line of desc.split('\n')) {
    const trimmed = line.trim()
    if (trimmed.startsWith('```')) { inCodeBlock = !inCodeBlock; continue }
    if (inCodeBlock || !trimmed || trimmed.startsWith('#') || trimmed.startsWith('---') || trimmed.startsWith('|')) continue
    return trimmed.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').slice(0, 300)
  }
  return desc.slice(0, 300)
}

export default function McpRegistryCard() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const [limit, setLimit] = useState(50)
  const [filter, setFilter] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const { data: servers = [], isLoading, isFetching, error, refetch } = useQuery<RegistryServer[]>({
    queryKey: ['mcp-registry'],
    queryFn: async () => { const r = await api.aimMcpRegistry(); return r.servers || [] },
  })

  // Cross-reference AIM's isInstalled flag against servers actually in
  // KiroCrew's scope.  AIM reports isInstalled=true if a server is installed
  // on the machine for *any* agent (e.g. sage-plus-service-mcp is scoped
  // to the `atlas` agent), which doesn't mean it's in KiroCrew's config.
  // The Installed Integrations table reads list_servers() which only sees
  // servers in KiroCrew's scopes; mirror that semantics here so the two
  // views agree on what "installed" means.
  const { data: mcpServers = [] } = useQuery<McpServer[]>({
    queryKey: ['mcp-servers'],
    queryFn: async () => await api.mcpServers(),
  })
  const inScope = useMemo(() => new Set(mcpServers.map(s => s.name)), [mcpServers])

  const selected = servers.find(s => s.id === selectedId) ?? null

  const install = useMutation({
    mutationFn: async (serverId: string) => {
      const r = await api.aimMcpInstall(serverId)
      if (r.error) throw new Error(r.error)
      return r
    },
    onSuccess: (_data, serverId) => {
      // Optimistically reflect the new scope state so the badge flips
      // instantly.  The subsequent invalidate refetches the source of truth.
      queryClient.setQueryData<McpServer[]>(['mcp-servers'], prev =>
        prev && !prev.some(s => s.name === serverId)
          ? [...prev, { name: serverId } as McpServer]
          : prev
      )
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      queryClient.invalidateQueries({ queryKey: ['mcp-registry'] })
    },
  })

  const uninstall = useMutation({
    mutationFn: async (serverId: string) => {
      const r = await api.aimMcpUninstall(serverId)
      if (r.error) throw new Error(r.error)
      return r
    },
    onSuccess: (_data, serverId) => {
      // Optimistically drop the server from KiroCrew scope so the badge
      // flips instantly.  The subsequent invalidate refetches truth.
      queryClient.setQueryData<McpServer[]>(['mcp-servers'], prev =>
        prev?.filter(s => s.name !== serverId) ?? []
      )
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      queryClient.invalidateQueries({ queryKey: ['mcp-registry'] })
    },
  })

  const dismiss = useCallback(() => setSelectedId(null), [])

  const filtered = servers.filter(s => !filter || (s.id + ' ' + (s.title || '') + ' ' + (s.description || '') + ' ' + s.tier).toLowerCase().includes(filter.toLowerCase()))

  const renderActions = (s: RegistryServer) => {
    // "Installed" = present in KiroCrew's scope, not AIM's machine-wide view.
    const installed = inScope.has(s.id)
    return (
    <div>
      {install.error && install.variables === s.id && <div className="text-[12px] text-danger mb-1.5">{(install.error as Error).message}</div>}
      {uninstall.error && uninstall.variables === s.id && <div className="text-[12px] text-danger mb-1.5">{(uninstall.error as Error).message}</div>}
      <div className="flex items-center justify-between">
      {installed
        ? <><span className="text-[12px] font-semibold text-ok"><Check className="lucide-inline" /> Installed</span>
          {uninstall.isPending && uninstall.variables === s.id
            ? <span className="text-[12px] text-danger animate-pulse">Removing…</span>
            : <Btn danger onClick={e => { e.stopPropagation(); if (confirm(`Uninstall "${s.id}"?`)) uninstall.mutate(s.id) }} disabled={uninstall.isPending}>Uninstall</Btn>
          }</>
        : install.isPending && install.variables === s.id
          ? <span className="text-[12px] text-accent animate-pulse">Installing…</span>
          : <button className="px-3 py-1 rounded-md text-[12px] font-semibold bg-accent/10 text-accent border border-accent/30 hover:bg-accent hover:text-accent-fg transition-all cursor-pointer" disabled={install.isPending} onClick={e => { e.stopPropagation(); install.mutate(s.id) }}>Install</button>
      }
      </div>
    </div>
    )
  }

  return (<>
    <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2 flex items-center gap-2">Browse Integrations <InfoTip text={`Browse and install MCP servers from the ${provider.labels.pluginRegistryName.toLowerCase()}. Click Install to add a server, then Apply & Restart to activate.`} /></h4>
    <Card>
      <div className="flex items-center gap-2 mb-3">
        <div className="relative max-w-[480px] flex-1">
          <SearchInput placeholder="Filter servers…" value={filter} onChange={e => setFilter(e.target.value)} />
          {filter && <button className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors cursor-pointer" onClick={() => setFilter('')} aria-label="Clear search">&times;</button>}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Btn onClick={() => refetch()} disabled={isFetching} aria-label="Refresh"><RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} /></Btn>
        </div>
      </div>
      {error && <div className="text-[13px] text-danger mb-2">Failed to load registry</div>}
      <div className="h-[500px] overflow-y-auto overflow-x-hidden p-1 -m-1">
        {isLoading ? <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3">{Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="rounded-lg border border-border bg-card shadow-sm p-3.5 min-h-[140px] flex flex-col">
            <div className="h-4 w-3/4 rounded-md animate-pulse mb-2" style={{ background: 'var(--border)', animationDelay: `${i * 80}ms` }} />
            <div className="h-3 w-1/2 rounded-md animate-pulse mb-3" style={{ background: 'var(--border)', opacity: 0.7, animationDelay: `${i * 80 + 100}ms` }} />
            <div className="space-y-1.5 flex-1">
              <div className="h-3 w-full rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.5, animationDelay: `${i * 80 + 200}ms` }} />
              <div className="h-3 w-5/6 rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.5, animationDelay: `${i * 80 + 300}ms` }} />
              <div className="h-3 w-2/3 rounded-md animate-pulse" style={{ background: 'var(--border)', opacity: 0.4, animationDelay: `${i * 80 + 400}ms` }} />
            </div>
            <div className="mt-2.5 pt-2 border-t border-border/50"><div className="h-7 w-16 rounded-md animate-pulse" style={{ background: 'var(--border)', animationDelay: `${i * 80 + 500}ms` }} /></div>
          </div>
        ))}</div> : filtered.length === 0 ? (
          <div className="text-[13px] text-muted py-2">{servers.length === 0 ? 'No servers in registry' : 'No matches'}</div>
        ) : <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3">{filtered.slice(0, limit).map(s => selected?.id === s.id ? (
          <div key={s.id} className="h-[180px] rounded-lg border border-transparent" />
        ) : (
          <motion.div key={s.id} layoutId={`mcp-card-${s.id}`} transition={{ type: 'spring', stiffness: 500, damping: 35 }} onClick={() => setSelectedId(s.id)} className="flex flex-col rounded-lg border border-border bg-card shadow-sm p-3.5 transition-[border-color,box-shadow] h-[180px] hover:border-border-strong hover:shadow-md cursor-pointer overflow-hidden">
            <div className="flex items-start justify-between gap-2 mb-1.5">
              <div className="min-w-0">
                <div className="text-[13px] font-semibold text-text leading-tight">{s.title || s.id}</div>
                <div className="text-[11px] text-muted font-mono mt-0.5">{s.id}</div>
              </div>
              {s.tier === 'Recommended' && <span className="px-1.5 py-[1px] rounded-full text-[11px] font-bold bg-accent/15 text-accent border border-accent/30 shrink-0 whitespace-nowrap"><Star className="lucide-inline" /> recommended</span>}
              {s.tier === 'Supported' && <span className="px-1.5 py-[1px] rounded-full text-[11px] font-bold bg-muted/15 text-muted border border-muted/30 shrink-0">supported</span>}
            </div>
            {s.description && (
              <div className="text-[12px] text-muted leading-relaxed flex-1 line-clamp-4 overflow-hidden">
                {summaryFromDescription(s.description)}
              </div>
            )}
            <div className="mt-2.5 pt-2 border-t border-border/50">
              {renderActions(s)}
            </div>
          </motion.div>
        ))}</div>}
        {filtered.length > limit && (
          <div
            role="button"
            tabIndex={0}
            className="flex justify-center py-3 mt-3 text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle rounded-md"
            onClick={() => setLimit(prev => prev + 50)}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setLimit(prev => prev + 50) } }}
          >
            Load more… ({filtered.length - limit} remaining)
          </div>
        )}
      </div>
    </Card>

    <McpDetailModal server={selected} onDismiss={dismiss} actions={selected ? renderActions(selected) : null} />
  </>)
}
