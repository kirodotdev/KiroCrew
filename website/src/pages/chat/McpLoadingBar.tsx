import { memo, useEffect, useId, useMemo, useState } from 'react'
import { Check, ChevronDown, ChevronRight, LoaderCircle } from 'lucide-react'
import { useAppSelector } from '../../store'
import { Glass } from '../../components/Glass'
import { MORE_BELOW_MASK } from '../../components/ModelEffortDropdown'
import { useScrollEdgesY } from '../../hooks/useScrollEdges'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { mcpSessionLoading, type McpSessionLoadRow, type McpSessionLoadState } from '../../lib/mcpSessionReport'
import { SESSION_DOT_CLASS } from './McpToolsPanel'

/** Each state's mark. The four the session-options MCP view already draws keep
 *  its rings, so a server wears the same mark in both lists; the two states
 *  that view has no ring for take its filled "switched off" dot. */
const STATE_DOT_CLASS: Record<McpSessionLoadState, string> = {
  connecting: SESSION_DOT_CLASS.no_report,
  needs_sign_in: SESSION_DOT_CLASS.awaiting_auth,
  failed: SESSION_DOT_CLASS.failed,
  disabled: 'bg-muted',
  registry_dropped: 'bg-muted',
  connected: SESSION_DOT_CLASS.started,
}
const STATE_LABEL_KEY: Record<McpSessionLoadState, string> = {
  connecting: 'pages.chat.mcpLoadingBar.state_connecting',
  needs_sign_in: 'pages.chatPage.mcp_session_awaiting_auth',
  failed: 'pages.chatPage.mcp_session_failed',
  disabled: 'pages.chat.mcpLoadingBar.state_disabled',
  registry_dropped: 'pages.chat.mcpLoadingBar.state_registry_dropped',
  connected: 'pages.chatPage.mcp_session_started',
}

function ServerRow({ row }: { row: McpSessionLoadRow }) {
  return (
    <li data-testid="mcp-loading-row" data-state={row.state} className="flex items-center gap-2 text-[12px] font-mono">
      <span className={`w-2 h-2 rounded-full shrink-0 ${STATE_DOT_CLASS[row.state]}`} aria-hidden="true" />
      <span className="min-w-0 truncate text-text" translate="no">{row.name}</span>
      <span className="ml-auto shrink-0 pl-2 text-[11px] text-muted">{i18nT(STATE_LABEL_KEY[row.state])}</span>
    </li>
  )
}

/**
 * "N of M MCP servers ready", as a small pill in the composer's status stack,
 * while any of the session's MCP servers is still connecting.
 *
 * A chat's first message does not wait for the person's own MCP servers, so for
 * a while the session has fewer tools than it will. This says so, and stops
 * saying so by itself: with nothing connecting it renders nothing, which is
 * every chat whose servers are up and every backend that does not report which
 * servers are still connecting.
 *
 * Only a `connecting` server shows it. The count is "connected of connected
 * plus connecting", and the open pill lists exactly those servers first, so the
 * rows under the count add up to it. A server that failed, waits for a sign-in,
 * is switched off or was left out is never "loading": it is in neither number,
 * and is listed as what it is in a second group that says it is not counted.
 * The session-options MCP view keeps its error and its reason.
 *
 * Reads `slot.mcp_report` off the shared slots array, which both the `slots`
 * snapshot and the live `mcp_report_update` delta fill, so it needs no request
 * of its own and survives a reload.
 *
 * An open pill stays until it is closed, even once everything connected: the
 * list a person is reading is not taken from under them.
 */
const McpLoadingBar = memo(function McpLoadingBar({ slot }: { slot: string | null }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const report = useAppSelector(s =>
    (slot ? (s.dashboard.slots ?? []).find(x => x.key === slot)?.mcp_report : null) ?? null,
  )
  const { connecting, ready, counted, uncounted, partial } = useMemo(() => mcpSessionLoading(report), [report])
  // Keyed by the slot it was opened in, so another session's pill starts closed.
  const [openSlot, setOpenSlot] = useState<string | null>(null)
  const expanded = slot !== null && openSlot === slot
  const listId = useId()
  const uncountedId = useId()
  // The list scrolls inside a fixed height; its bottom edge fades while rows sit
  // below it, so a list cut at a row boundary does not read as complete.
  const [attachList, listEdges, , attachListRows] = useScrollEdgesY<HTMLDivElement>()

  const loading = connecting.length > 0
  // Open with nothing connecting, it keeps its list; with no server left to
  // list (the session was reset and its report cleared) there is nothing to keep.
  const listed = counted.length + uncounted.length > 0
  // A cleared report closes it: after a reset the next session in this slot
  // starts closed, so a session with nothing connecting shows nothing.
  // shortcut: the open state is keyed by slot, not by session, so a session
  // replaced without this pill drawing a cleared report (a reset while another
  // slot is shown, a respawn) keeps it open, listing the new session's servers;
  // key it by session once the report names its session.
  useEffect(() => { if (expanded && !listed) setOpenSlot(null) }, [expanded, listed])
  if (!slot || (!loading && !(expanded && listed))) return null

  const count = i18nT('pages.chat.mcpLoadingBar.ready_of_total', {
    ready: fmtNumber(ready.length),
    total: fmtNumber(counted.length),
  })

  return (
    // `relative z-[2]` clears the transcript's bottom mask, as the task pill does.
    <div className="px-4 mx-auto w-full relative z-[2]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <Glass
        variant="chip"
        thickness="thick"
        radius={expanded ? 8 : 16}
        className={`mb-1 animate-slide-up ${expanded ? '' : 'inline-flex max-w-full'}`}
      >
        <div className="min-w-0 overflow-hidden rounded-[inherit]">
          <button
            type="button"
            data-testid="mcp-loading-pill"
            onClick={() => setOpenSlot(expanded ? null : slot)}
            aria-expanded={expanded}
            aria-controls={expanded ? listId : undefined}
            className={`flex items-center gap-2 py-1.5 text-[13px] font-mono text-muted bg-transparent border-none cursor-pointer hover:bg-accent/5 transition-colors focus-ring-accent-inset ${
              expanded ? 'w-full px-3' : 'px-3 min-w-0'
            }`}
          >
            {expanded
              ? <ChevronDown size={14} className="shrink-0" aria-hidden="true" />
              : <ChevronRight size={14} className="shrink-0" aria-hidden="true" />}
            {/* The spinner turns only where motion is welcome; the sentence
                beside it says the same thing without it. */}
            {loading
              ? <LoaderCircle size={14} className="shrink-0 motion-safe:animate-spin" aria-hidden="true" data-testid="mcp-loading-spinner" />
              : <Check size={14} className="shrink-0 text-ok" aria-hidden="true" />}
            <span className="truncate tabular-nums" data-testid="mcp-loading-count">{count}</span>
          </button>
          {/* Said once, when the pill appears. The count above is the button's
              own name, not a live region, so each server that connects is not
              read out. */}
          {loading && (
            <span className="sr-only" role="status" data-testid="mcp-loading-status">
              {i18nT('pages.chat.mcpLoadingBar.still_connecting')}
            </span>
          )}
          {expanded && (
            <div
              id={listId}
              ref={attachList}
              data-testid="mcp-loading-scroller"
              className={`px-3 pb-2 max-h-64 overflow-y-auto ${listEdges.bottom ? MORE_BELOW_MASK : ''}`}
            >
              {/* Rows come and go as servers connect: their box is what the edge check follows. */}
              <div ref={attachListRows}>
              <ul
                data-testid="mcp-loading-list"
                aria-label={i18nT('pages.chat.mcpLoadingBar.list_label')}
                className="space-y-0.5 list-none m-0 p-0"
              >
                {counted.map(row => <ServerRow key={row.name} row={row} />)}
              </ul>
              {/* Apart, under their own line: the count above is not about these. */}
              {uncounted.length > 0 && (
                <>
                  <div
                    id={uncountedId}
                    data-testid="mcp-loading-uncounted-label"
                    className="mt-1.5 pt-1.5 mb-0.5 border-t border-border text-[11px] text-muted font-mono"
                  >
                    {i18nT('pages.chat.mcpLoadingBar.not_counted', { n: fmtNumber(uncounted.length) })}
                  </div>
                  <ul
                    data-testid="mcp-loading-uncounted"
                    aria-labelledby={uncountedId}
                    className="space-y-0.5 list-none m-0 p-0"
                  >
                    {uncounted.map(row => <ServerRow key={row.name} row={row} />)}
                  </ul>
                </>
              )}
              {partial && (
                <div className="mt-1 text-[11px] text-muted font-mono" data-testid="mcp-loading-untracked">
                  {i18nT('pages.chat.mcpLoadingBar.untracked')}
                </div>
              )}
              </div>
            </div>
          )}
        </div>
      </Glass>
    </div>
  )
})

export default McpLoadingBar
