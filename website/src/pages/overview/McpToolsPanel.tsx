import type { ReactNode } from 'react'
import { motion } from 'framer-motion'
import { i18nT } from '../../i18n/t'
import McpToolAlwaysAllow, { McpToolGrantsCount, McpToolGrantsNote } from './McpToolAlwaysAllow'

/** A server's open tool list: the full-width row under that server's row in the
 *  MCP Servers table, its tools in as many columns as the table is wide (one on
 *  a phone). The panel is pinned to the scroller's left edge at the scroller's
 *  own width (`100cqw` of the `@container` wrapping the scroller), so a table wider than the
 *  screen never pushes the tools sideways. */
export default function McpToolsPanel({ id, labelledBy, edgeClassName, server, tools, disabledTools, pendingTools, serverOff, locked, notice, onToggleTool }: {
  id: string
  /** Ids naming the group: the server's name and its `N tools` button. */
  labelledBy: string
  /** The row's staged-change edge, as an inset shadow on the panel. */
  edgeClassName?: string
  server: string
  tools: string[]
  disabledTools: string[]
  /** Staged tool switches for this server, by tool, until Apply. */
  pendingTools: Record<string, boolean> | undefined
  /** The server is off in its config: no Always allow switches. */
  serverOff: boolean
  /** A staged uninstall or an off server: the tool switches are inert. */
  locked: boolean
  /** What one tool-off costs on this harness, read before any click. */
  notice: ReactNode
  onToggleTool: (tool: string, enabled: boolean) => void
}) {
  const enabledOf = (t: string) => pendingTools?.[t] ?? !disabledTools.includes(t)
  return (
    <motion.div
      id={id}
      data-testid="mcp-tools-panel"
      role="group"
      aria-labelledby={labelledBy}
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.2 }}
      className={`sticky left-0 w-[100cqw] overflow-hidden ${edgeClassName ?? ''}`}
    >
      <div className="px-2.5 pt-1 pb-2.5">
        {notice}
        <McpToolGrantsNote server={server} />
        {!serverOff && <McpToolGrantsCount server={server} tools={tools.filter(enabledOf)} />}
        <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,18rem),1fr))] items-start gap-x-6 gap-y-1">{tools.map(t => {
          const effectivelyEnabled = enabledOf(t)
          const hasPending = pendingTools?.[t] !== undefined
          return <div key={t} className="grid grid-cols-[minmax(0,1fr)_auto] content-start items-center gap-x-2 gap-y-0.5 min-w-0"><button
            type="button"
            className={`flex items-center gap-1.5 min-w-0 text-[12px] font-mono break-all cursor-pointer transition-colors bg-transparent border-none p-0 text-left ${effectivelyEnabled ? 'text-text hover:text-accent' : 'text-muted line-through opacity-60 hover:opacity-100'} ${hasPending ? 'ring-1 ring-[var(--warn)] rounded px-1' : ''}`}
            disabled={locked}
            onClick={() => onToggleTool(t, !effectivelyEnabled)}
            title={hasPending
              ? (effectivelyEnabled ? i18nT('pages.overview.mcpTab.pending_enable_click_to_revert') : i18nT('pages.overview.mcpTab.pending_disable_click_to_revert'))
              : effectivelyEnabled ? i18nT('pages.overview.mcpTab.click_to_disable_pending_until_apply') : i18nT('pages.overview.mcpTab.click_to_enable_pending_until_apply')}
          ><span className={`w-1.5 h-1.5 rounded-full shrink-0 ${effectivelyEnabled ? 'bg-ok' : 'bg-muted'}`} />{t}</button>
          {/* Beside Disable: stop asking for this one tool (the approval card's grant). */}
          {effectivelyEnabled && !serverOff && <McpToolAlwaysAllow server={server} tool={t} />}</div>
        })}</div>
      </div>
    </motion.div>
  )
}
