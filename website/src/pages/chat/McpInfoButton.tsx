import { useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { Plug, Zap } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'

import { i18nT } from '../../i18n/t'
export default function McpInfoButton({ agent }: { agent?: string }) {
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Both reads run only while the popover is open and re-run on every open
  // (`enabled` flips with `open`; `staleTime: 0` opts out of the client's
  // `Infinity` default, which would otherwise serve the previous open's cache
  // without a refetch), so the answer is fresh without a hand-rolled effect.
  // Same keys as ChatPage's MCP dropdown, so the two surfaces share one cache.
  // A refused read is `isError` — previously a failed fetch left the popover
  // on "None loaded", a claim about the session rather than about the request.
  const serversQ = useQuery<{ name: string; enabled?: boolean }[]>({
    queryKey: ['mcp-servers', agent],
    queryFn: () => api.mcpActive(agent || undefined),
    enabled: open,
    staleTime: 0,
    retry: false,
  })
  // Tool Search mode: when on, MCP tool specs are deferred (search-and-call)
  // so servers show connected but their tools load only when used.
  const toolSearchQ = useQuery<{ agent?: { tool_search?: boolean } }, Error, boolean>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    select: (c) => c.agent?.tool_search ?? true,
    enabled: open,
    staleTime: 0,
    retry: false,
  })
  // On a refused list read the previous open's data is not shown either: a
  // stale list under "couldn't load" would read as the current answer.
  const serversFailed = serversQ.isError
  const servers = serversFailed ? [] : (serversQ.data ?? [])
  const configFailed = toolSearchQ.isError
  const toolSearchOn = toolSearchQ.data ?? true

  useEffect(() => {
    return () => { if (closeTimer.current) clearTimeout(closeTimer.current) }
  }, [])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      if (btnRef.current?.contains(e.target as Node) || popoverRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => { document.removeEventListener('pointerdown', onPointerDown); document.removeEventListener('keydown', onKeyDown) }
  }, [open])

  const handleEnter = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    setOpen(true)
  }
  const handleLeave = () => {
    closeTimer.current = setTimeout(() => setOpen(false), 250)
  }

  return (
    <>
      <button ref={btnRef} onClick={() => setOpen(o => !o)} onMouseEnter={handleEnter} onMouseLeave={handleLeave} className="shrink-0 w-5 h-5 rounded flex items-center justify-center text-muted hover:text-text hover:bg-bg-hover transition-all" title={i18nT('pages.chat.mcpInfoButton.session_mcp_servers')} aria-label={i18nT('pages.chat.mcpInfoButton.session_mcp_servers')}><Plug size={15} /></button>
      {open && btnRef.current && createPortal(
        // The mouse handlers only keep this hover popover open while the pointer
        // is over it; the popover is presentational (its trigger button owns all
        // interaction), so it is not a keyboard/interactive target itself.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div ref={popoverRef} role="tooltip" onMouseEnter={handleEnter} onMouseLeave={handleLeave} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg p-3 min-w-[240px] max-w-[300px] max-h-[320px] overflow-y-auto" style={(() => { const r = btnRef.current!.getBoundingClientRect(); const top = r.bottom + 4 + 320 > window.innerHeight ? r.top - 320 - 4 : r.bottom + 4; const left = Math.max(8, Math.min(r.left, window.innerWidth - 308)); return { top, left } })()}>
          <div className="text-[12px] uppercase tracking-wider text-muted font-semibold mb-2">{i18nT('pages.chat.mcpInfoButton.mcp_servers_count', { count: servers.filter(s => s.enabled !== false).length, total: servers.length })}</div>
          <div className="flex items-center gap-1.5 text-[11px] mb-0.5">
            <Zap size={11} className={toolSearchOn ? 'text-ok' : 'text-[var(--warn)]'} />
            <span className={`font-medium ${toolSearchOn ? 'text-ok' : 'text-[var(--warn)]'}`}>{toolSearchOn ? i18nT('pages.chatPage.tool_search_deferred') : i18nT('pages.chatPage.tool_search_full')}</span>
          </div>
          <div className="text-[11px] text-muted mb-2 leading-snug">{toolSearchOn ? i18nT('pages.chatPage.tool_search_deferred_hint') : i18nT('pages.chatPage.tool_search_full_hint')}</div>
          {/* Read failures in a hover popover, nothing unsaved → hand-off on.
              Each names the read that failed; the mode line above still shows
              the default when the probe failed, so say so rather than let it
              pass as a measurement. */}
          {configFailed && (
            <ErrorNotice variant="inline" className="mb-2" message={i18nT('pages.chat.mcpInfoButton.mode_load_failed')} askAgent />
          )}
          {serversFailed && (
            <ErrorNotice variant="inline" className="mb-2" message={i18nT('pages.chat.mcpInfoButton.load_failed')} askAgent />
          )}
          {!serversFailed && (servers.length === 0 ? <div className="text-muted text-[13px] italic">{i18nT('pages.chat.mcpInfoButton.none_loaded')}</div> : servers.map(s => (
            <div key={s.name} className={`flex items-center gap-2 py-1 text-[13px] ${s.enabled === false ? 'opacity-40' : ''}`}>
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${s.enabled === false ? 'bg-muted' : 'bg-ok'}`} />
              <code className="text-text">{s.name}</code>
              {s.enabled === false && <span className="text-[11px] text-muted">{i18nT('pages.chat.mcpInfoButton.disabled')}</span>}
            </div>
          )))}
          <div className="mt-2 pt-2 border-t border-border text-[11px] text-muted leading-snug">
            {agent && agent !== 'kirocrew'
              ? i18nT('pages.chat.mcpInfoButton.agent_loads_only_its_own_mcp_servers', { name: agent })
              : 'kirocrew loads all configured MCP servers — manage from Overview → MCP tab.'}
          </div>
        </div>,
        document.body
      )}
    </>
  )
}
