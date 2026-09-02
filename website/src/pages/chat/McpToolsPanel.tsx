import { useState } from 'react'
import { Zap, ChevronRight } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { mcpToolStatus, type McpToolStatus } from '../../lib/mcpLoadedTools'
import {
  mcpSessionExtraServers,
  mcpSessionFailureReason,
  mcpSessionHasReport,
  mcpSessionServerState,
  type McpSessionServerState,
} from '../../lib/mcpSessionReport'
import type { McpSessionReport } from '../../types'

export interface McpServerLite {
  name: string
  enabled?: boolean
}
export interface McpToolsInfo {
  tools?: string[]
  disabledTools?: string[]
}

/** Status dot styling per tool state (theme tokens only — no hardcoded color). */
export const DOT_CLASS: Record<McpToolStatus, string> = {
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
 * Per-server indicator for what THIS session reported.
 *
 * A RING, where the tool dots are filled circles: the two vocabularies share
 * this panel and colour is already spent on state, so shape has to carry which
 * question a mark answers. Without that, "loaded this session" and "started in
 * this session" are the same green dot and "deferred" and "no report yet" are
 * the same hollow one — four labels, two marks, and a reader cannot tell which
 * legend governs which row.
 *
 * `no_report` is dashed rather than solid because it means "not known", not "a
 * reported absence" — the drain is time bounded and a late frame still arrives.
 */
export const SESSION_DOT_CLASS: Record<McpSessionServerState, string> = {
  started: 'border-2 border-ok',
  failed: 'border-2 border-danger',
  awaiting_auth: 'border-2 border-warn',
  no_report: 'border border-dashed border-muted',
}
const SESSION_LABEL_KEY: Record<McpSessionServerState, string> = {
  started: 'pages.chatPage.mcp_session_started',
  failed: 'pages.chatPage.mcp_session_failed',
  awaiting_auth: 'pages.chatPage.mcp_session_awaiting_auth',
  no_report: 'pages.chatPage.mcp_session_no_report',
}

/**
 * The chat session-options dropdown's MCP view: a "MCP Servers (n/n)" header,
 * the Tool Search mode line, and an expandable per-server list.
 *
 * TWO different facts are on screen, and keeping them distinguishable is the
 * point. The server LIST and its enabled flags are configuration — the agent
 * spec on disk, from `/api/mcp/active` — so they describe the host, not this
 * session. `sessionReport` is the session's own answer, published by the backend
 * from the registration frames its ACP session actually received; when it is
 * present each server's leading dot shows that instead. A server with no report
 * renders as UNREPORTED, never as absent: the backend's drain is time bounded
 * and a late frame still arrives, so claiming absence here would reintroduce the
 * false authority this split exists to remove.
 *
 * The tool rows' loaded/deferred/disabled dot is a third thing again — a
 * client-side approximation derived from this session's tool_search results (see
 * deriveLoadedMcpTools), which starts empty after a reload.
 */
export default function McpToolsPanel({
  servers,
  toolsByServer,
  loaded,
  toolSearchOn,
  loading,
  sessionReport,
}: {
  servers: McpServerLite[]
  toolsByServer: Record<string, McpToolsInfo>
  loaded: Set<string>
  toolSearchOn: boolean
  loading: boolean
  sessionReport?: McpSessionReport | null
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
  const hasSessionReport = mcpSessionHasReport(sessionReport)
  const extraServers = hasSessionReport
    ? mcpSessionExtraServers(servers.map(s => s.name), sessionReport)
    : []

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
      {!loading && servers.length > 0 && hasSessionReport && (
        <div className="text-[10px] text-muted mb-2 leading-snug">
          {i18nT('pages.chatPage.mcp_session_source_note')}
        </div>
      )}
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
      {!loading && servers.length > 0 && hasSessionReport && (
        // The tool rows' dots and the server rows' dots share a shape but not a
        // vocabulary, so the tool legend alone would invite reading a server dot
        // as a tool state. No `title` on these swatches: the per-server dot owns
        // that, and duplicating it would make the tooltip ambiguous.
        <div className="flex items-center gap-2.5 flex-wrap text-[10px] text-muted mb-2">
          {(
            ['started', 'failed', 'awaiting_auth', 'no_report'] as McpSessionServerState[]
          ).map(st => (
            <span key={st} className="inline-flex items-center gap-1">
              <span className={`w-2 h-2 rounded-full ${SESSION_DOT_CLASS[st]}`} />
              {i18nT(SESSION_LABEL_KEY[st])}
            </span>
          ))}
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
          const sessionState = mcpSessionServerState(s.name, sessionReport)
          const sessionReason = mcpSessionFailureReason(s.name, sessionReport)
          const sessionLabel = i18nT(SESSION_LABEL_KEY[sessionState])
          // With a report in hand the mark answers "did this start HERE" and is
          // drawn as a ring; without one it falls back to the configured enabled
          // flag as a filled dot, which is all the dashboard used to know. The
          // row's own opacity still carries disabled.
          const serverDotClass = hasSessionReport
            ? `w-2 h-2 ${SESSION_DOT_CLASS[sessionState]}`
            : `w-1.5 h-1.5 ${serverDim ? 'bg-muted' : 'bg-ok'}`
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
                <span
                  className={`rounded-full shrink-0 ${serverDotClass}`}
                  title={
                    hasSessionReport
                      ? sessionReason
                        ? `${sessionLabel}: ${sessionReason}`
                        : sessionLabel
                      : undefined
                  }
                />
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
      {extraServers.length > 0 && (
        // Session truth the configured list structurally cannot show: the backend
        // starts the agent spec's own servers as well as the ones Kiro Crew
        // injects on the wire, so dropping these would leave the same blind spot
        // pointing the other way.
        //
        // Deliberately NOT gated on `loading`: the caller passes
        // `loading={servers.length === 0}`, which is also true when the host
        // simply has NO configured servers — so gating here hid a session's real
        // servers permanently in exactly the case where they are the only thing
        // there is to show.
        <div className="mt-2 pt-2 border-t border-border text-[10px] text-muted leading-snug">
          {i18nT('pages.chatPage.mcp_session_also_started', { names: extraServers.join(', ') })}
        </div>
      )}
    </div>
  )
}
