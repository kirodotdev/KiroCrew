import { useState } from 'react'
import { Zap, ChevronRight } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { mcpToolStatus, type McpToolStatus } from '../../lib/mcpLoadedTools'

export interface McpServerLite {
  name: string
  enabled?: boolean
}
export interface McpToolsInfo {
  tools?: string[]
  disabledTools?: string[]
}

/** Status dot styling per tool state (theme tokens only — no hardcoded color). */
const DOT_CLASS: Record<McpToolStatus, string> = {
  active: 'bg-ok', // loaded this session
  deferred: 'bg-transparent border border-border', // hollow — spec not sent yet
  disabled: 'bg-muted', // turned off for this server
}
const STATUS_LABEL_KEY: Record<McpToolStatus, string> = {
  active: 'pages.chatPage.tool_status_loaded',
  deferred: 'pages.chatPage.tool_status_deferred',
  disabled: 'pages.chatPage.tool_status_disabled',
}

/**
 * Session MCP view shared by the chat session-options dropdown and the plug
 * popover: a "MCP Servers (n/n)" header, the Tool Search mode line, and an
 * expandable per-server list whose tool rows carry a loaded/deferred/disabled
 * dot. The loaded set is derived client-side from the session's tool_search
 * results (see deriveLoadedMcpTools) — this panel just renders it, so it takes
 * `loaded` as a prop and stays free of store/query concerns (testable in
 * isolation).
 */
export default function McpToolsPanel({
  servers,
  toolsByServer,
  loaded,
  toolSearchOn,
  loading,
}: {
  servers: McpServerLite[]
  toolsByServer: Record<string, McpToolsInfo>
  loaded: Set<string>
  toolSearchOn: boolean
  loading: boolean
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggle = (name: string) =>
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  const enabledCount = servers.filter(s => s.enabled !== false).length

  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-muted font-semibold mb-1.5">
        {i18nT('pages.chatPage.mcp_servers_2')} {servers.length > 0 && `(${enabledCount}/${servers.length})`}
      </div>
      <div className="flex items-center gap-1.5 text-[11px] mb-0.5">
        <Zap size={11} className={toolSearchOn ? 'text-ok' : 'text-muted'} />
        <span className={`font-medium ${toolSearchOn ? 'text-ok' : 'text-muted'}`}>
          {toolSearchOn ? i18nT('pages.chatPage.tool_search_deferred') : i18nT('pages.chatPage.tool_search_full')}
        </span>
      </div>
      <div className="text-[11px] text-muted mb-2 leading-snug">
        {toolSearchOn ? i18nT('pages.chatPage.tool_search_deferred_hint') : i18nT('pages.chatPage.tool_search_full_hint')}
      </div>
      {!loading && servers.length > 0 && (
        <div className="flex items-center gap-2.5 flex-wrap text-[10px] text-muted mb-2">
          <span className="inline-flex items-center gap-1">
            <span className={`w-1.5 h-1.5 rounded-full ${DOT_CLASS.active}`} />
            {i18nT('pages.chatPage.tool_status_loaded')}
          </span>
          <span className="inline-flex items-center gap-1">
            <span className={`w-1.5 h-1.5 rounded-full ${DOT_CLASS.deferred}`} />
            {i18nT('pages.chatPage.tool_status_deferred')}
          </span>
          <span className="inline-flex items-center gap-1">
            <span className={`w-1.5 h-1.5 rounded-full ${DOT_CLASS.disabled}`} />
            {i18nT('pages.chatPage.tool_status_disabled')}
          </span>
        </div>
      )}
      {loading ? (
        <div className="text-muted text-[12px] italic">{i18nT('pages.chatPage.loading')}</div>
      ) : (
        servers.map(s => {
          const info = toolsByServer[s.name] || {}
          const tools = info.tools || []
          const isOpen = expanded.has(s.name)
          const enabledTools = tools.filter(t => !(info.disabledTools || []).includes(t))
          const totalLoadable = enabledTools.length
          const loadedN = toolSearchOn
            ? enabledTools.filter(
                t => mcpToolStatus(s.name, t, { loaded, disabledTools: info.disabledTools, toolSearchOn }) === 'active',
              ).length
            : totalLoadable
          const serverDim = s.enabled === false
          const toggleRow = () => {
            if (tools.length) toggle(s.name)
          }
          return (
            <div key={s.name} className={serverDim ? 'opacity-40' : ''}>
              <button
                type="button"
                tabIndex={0}
                onClick={e => {
                  e.preventDefault()
                  e.stopPropagation()
                  toggleRow()
                }}
                onKeyDown={e => {
                  // Radix menu roving-focus manages arrow keys; handle Enter/Space
                  // here so keyboard users can expand a server row.
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    e.stopPropagation()
                    toggleRow()
                  }
                }}
                className="w-full flex items-center gap-2 py-0.5 text-[12px] bg-transparent border-none p-0 text-left cursor-pointer"
                aria-expanded={tools.length ? isOpen : undefined}
              >
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${serverDim ? 'bg-muted' : 'bg-ok'}`} />
                <code className="text-text flex-1">{s.name}</code>
                {totalLoadable > 0 && toolSearchOn && (
                  <span className="text-[10px] text-muted tabular-nums">
                    {loadedN}/{totalLoadable}
                  </span>
                )}
                {tools.length > 0 && (
                  <ChevronRight size={11} className={`text-muted transition-transform ${isOpen ? 'rotate-90' : ''}`} />
                )}
              </button>
              {isOpen && (
                <div className="ml-3.5 mb-1 space-y-0.5">
                  {tools.length === 0 ? (
                    <div className="text-[11px] text-muted italic">{i18nT('pages.chatPage.no_tools')}</div>
                  ) : (
                    tools.map(t => {
                      const st = mcpToolStatus(s.name, t, {
                        loaded,
                        disabledTools: info.disabledTools,
                        toolSearchOn,
                      })
                      const textCls =
                        st === 'disabled'
                          ? 'text-muted line-through opacity-60'
                          : st === 'active'
                            ? 'text-text'
                            : 'text-muted'
                      return (
                        <div
                          key={t}
                          className={`flex items-center gap-1.5 text-[11px] font-mono ${textCls}`}
                          title={i18nT(STATUS_LABEL_KEY[st])}
                        >
                          <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${DOT_CLASS[st]}`} />
                          {t}
                        </div>
                      )
                    })
                  )}
                </div>
              )}
            </div>
          )
        })
      )}
    </div>
  )
}
