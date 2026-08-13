import { useEffect, useRef, useState, useCallback, useMemo, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Bot, ScrollText, X, Lock, CheckCircle, AlertCircle, Loader as LoaderIcon, Ban, Handshake, Wrench, MessageCircleQuestionMark, Workflow, BookmarkPlus, Component, GitPullRequest, CircleDot, Square, RotateCcw, Clock, Search, Link as LinkIcon, ExternalLink } from 'lucide-react'
import { api } from '../../api/client'
import { LogViewer } from '../LogsPage'
import TrustDropdown from '../../components/TrustDropdown'
import Clickable from '../../components/Clickable'
import type { SubagentActivity, ToolActivity, Artifact } from '../../types'
import { countDiffStats } from '../../utils/diffLineCounts'
import type { ExtractedLink } from '../../utils/extractChatLinks'
import { dedupResourceLinks, resourceKey } from '../../utils/extractChatLinks'
import type { PullRequestLink } from '../../utils/pullRequestLinks'
import PullRequestPanel from '../../components/PullRequestPanel'
import IssuePanel from '../../components/IssuePanel'
import { PinnedMessagesPanel } from './PinnedMessagesPanel'
import type { ChatPin } from '../../api/pins'
import { useAppSelector, useAppDispatch } from '../../store'
import { markSubagentApproving, openActivityToTab, selectSubagent, clearTerminalSubagents, sseSubagentDone } from '../../store/chatSlice'
import SegmentedControl from '../../components/SegmentedControl'
import { PanelSectionHeader } from '../../components/ui'
import SideChat from './SideChat'
import WorkflowSidebarRow, { type WfRunRow } from './WorkflowSidebarRow'
import { runBelongsToSlot } from '../../apps/workflows/runModel'

import { ContextBreakdownTab } from '../ContextBreakdownPanel'
import SessionSummaryTab from './SessionSummaryTab'
import { i18nT } from '../../i18n/t'
import GitPanel from '../../components/GitPanel'
import { fmtDateFields } from '../../i18n/format'
const STATUS = {
  pending: <Lock size={12} className="text-muted" />,
  running: <LoaderIcon size={12} className="text-accent animate-spin" />,
  tool: <Wrench size={12} className="text-amber-400" />,
  done: <CheckCircle size={12} className="text-green-400" />,
  error: <AlertCircle size={12} className="text-danger" />,
  stopped: <Square size={12} className="text-muted" />,
} as const

// Resource-link type ('cr' | 'issue' | 'other', from extractChatLinks) is
// encoded on the ResourceRow ICON — a pull-request glyph in accent for code
// reviews, a filled-dot glyph in ok for provider issues, a link glyph in muted
// for everything else — rather than a leading text badge. An icon is
// fixed-width, so every row's label starts at the same left text edge; a
// badge's width varies with its label and would ragged them. The icon is the
// only VISUAL type signal, so each row also carries the type as sr-only text —
// translated, because it is the only signal a screen-reader user gets.
const resourceTypeLabel = (type: string): string =>
  type === 'cr' ? i18nT('pages.chat.activityViewer.resource_type_pr')
    : type === 'issue' ? i18nT('pages.chat.activityViewer.resource_type_issue')
      : i18nT('pages.chat.activityViewer.resource_type_link')

function fmtTime(ts: number) {
  return fmtDateFields(ts, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/* ── Subagent pane ── */

/** Lazy-load subagent output from disk on demand (memory-friendly).
 *  Backend GET /api/spawn/{id} applies _redact() (redact_exfiltration_urls + redact_credentials)
 *  — see messaging.py:api_spawn_status line 109. */
function DiskLoader({ id, autoLoad }: { id: string; autoLoad?: boolean }) {
  const [text, setText] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(false)
  const ctrlRef = useRef<AbortController | null>(null)
  useEffect(() => () => { ctrlRef.current?.abort() }, [])
  const load = useCallback(() => {
    ctrlRef.current?.abort()
    const ctrl = ctrlRef.current = new AbortController()
    setLoading(true); setError(false)
    api.spawnStatus(id, { signal: ctrl.signal })
      .then(d => { if (!ctrl.signal.aborted) setText(d.result || '(no output)') })
      .catch(() => { if (!ctrl.signal.aborted) setError(true) })
      .finally(() => { if (!ctrl.signal.aborted) setLoading(false) })
  }, [id])
  // 1-click transcript: a chip-selected card loads its output immediately
  // instead of waiting for the manual button press.
  useEffect(() => {
    if (autoLoad && text === null && !loading && !error) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoLoad])
  if (text !== null) return <>{text}</>
  if (loading) return <span className="text-muted/30 italic">{i18nT('pages.chat.activityViewer.loading')}</span>
  if (error) return <button className="text-danger/70 hover:text-danger text-[12px] underline cursor-pointer bg-transparent border-none p-0 font-mono" onClick={e => { e.stopPropagation(); load() }}>{i18nT('pages.chat.activityViewer.failed_click_to_retry')}</button>
  return <button className="text-accent/70 hover:text-accent text-[12px] underline cursor-pointer bg-transparent border-none p-0 font-mono" onClick={e => { e.stopPropagation(); load() }}>{i18nT('pages.chat.activityViewer.load_output_from_disk')}</button>
}

function SubagentPane({ a, slot, onClick, selected }: { a: SubagentActivity; slot: string; onClick: () => void; selected?: boolean }) {
  const bodyRef = useRef<HTMLPreElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const autoScroll = useRef(true)
  const isPending = a.status === 'pending'
  const isDone = a.status === 'done' || a.status === 'error' || a.status === 'stopped'
  // Native cards have no SubagentManager record to lazy-load from disk; their
  // output arrives inline on the done event (a.result).
  const isNative = a.id.startsWith('native:')
  const [collapsed, setCollapsed] = useState(isDone)
  // Auto-collapse when transitioning to done (not on mount)
  const wasDone = useRef(isDone)
  useEffect(() => {
    if (isDone && !wasDone.current) { const t = setTimeout(() => setCollapsed(true), 2000); wasDone.current = true; return () => clearTimeout(t) }
  }, [isDone])
  const isRunning = a.status === 'running' || a.status === 'tool'

  // Approval handling for pending subagents
  const dispatch = useAppDispatch()
  // 1-click transcript: chip selection expands the card, scrolls it into
  // view, and (via DiskLoader autoLoad) fetches the output — then clears the
  // selection so a later re-click re-triggers.
  useEffect(() => {
    if (!selected) return
    setCollapsed(false)
    cardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    const t = setTimeout(() => dispatch(selectSubagent(null)), 800)
    return () => clearTimeout(t)
  }, [selected, dispatch])
  const onApprove = useCallback((e: React.MouseEvent, action: 'approve' | 'reject') => {
    e.stopPropagation()
    if (!a.approval_id) return
    dispatch(markSubagentApproving({ id: a.id, approving: true }))
    api.resolveApproval(a.approval_id, action).then(() => {
      // See the matching note in ChatInput's resolveOneSpawn: the backend's
      // `approval_resolved` frame carries no slot, so the WS handler that would
      // terminate the card is skipped. An approved spawn converges on its own
      // spawn/chunk/done stream; a rejected one never runs and emits nothing
      // further, leaving the card stuck on "Resolving…" without this dispatch.
      if (action === 'reject' && slot) {
        dispatch(sseSubagentDone({ slot, id: a.id, elapsed: 0, error: 'rejected' }))
      }
    }).catch(() => dispatch(markSubagentApproving({ id: a.id, approving: false })))
  }, [a.approval_id, a.id, slot, dispatch])

  // Live elapsed timer for running subagents
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!isRunning) return
    const tick = () => setElapsed(Math.floor((Date.now() - a.startedAt) / 1000))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [isRunning, a.startedAt])

  useEffect(() => {
    const el = bodyRef.current
    if (el && autoScroll.current) el.scrollTop = el.scrollHeight
  }, [a.streaming, a.lastTool])

  const onScroll = useCallback(() => {
    const el = bodyRef.current
    if (!el) return
    autoScroll.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 20
  }, [])

  const onCancel = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    api.spawnDelete(a.id).catch(() => {})
  }, [a.id])

  const displayElapsed = isRunning ? elapsed : Math.round(a.elapsed || 0)
  const fmtElapsed = displayElapsed >= 60 ? `${Math.floor(displayElapsed / 60)}m ${displayElapsed % 60}s` : `${displayElapsed}s`

  // Inside the Subagents tab the "Subagent" prefix is redundant, and in a
  // narrow rail it was the part that survived truncation while the actual
  // status got clipped. Show the status; keep the full phrase as the tooltip.
  const statusLabel = isPending
    ? i18nT('pages.chat.activityViewer.pending_approval')
    : a.status === 'tool' ? i18nT('pages.chat.activityViewer.running_tool')
      : a.status === 'running' ? (a.streaming ? i18nT('pages.chat.activityViewer.running') : i18nT('pages.chat.activityViewer.starting'))
        : a.status === 'done' ? i18nT('pages.chat.activityViewer.complete')
          : a.status === 'stopped' ? i18nT('pages.chat.activityViewer.stopped')
            : a.error?.includes('Cancelled') ? i18nT('pages.chat.activityViewer.cancelled') : i18nT('pages.chat.activityViewer.error')

  return (
    // Card-level mouse convenience that selects the subagent; it wraps its own
    // interactive controls (Cancel, collapse header) which carry the real
    // keyboard/AT semantics. The outer div carries the scroll-to anchor for
    // chip-selected cards.
    <div ref={cardRef}>
    <Clickable className={`mx-2 mb-3 rounded-lg border bg-card overflow-hidden shadow-sm transition-all animate-scale-in ${isRunning || isPending ? 'border-border-strong' : 'border-border opacity-60'}${selected ? ' ring-1 ring-accent' : ''}`} onClick={onClick}>
      {/* Header — collapse toggle when the subagent is done */}
      <div
        className={`flex items-center gap-2 px-3 py-2.5${isDone ? ' cursor-pointer select-none hover:bg-bg-hover transition-colors' : ''}`}
        {...(isDone
          ? {
              role: 'button' as const,
              tabIndex: 0,
              'aria-expanded': !collapsed,
              onClick: () => setCollapsed(c => !c),
              onKeyDown: (e: React.KeyboardEvent) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setCollapsed(c => !c) }
              },
            }
          : {})}
      >
        <span className="shrink-0 flex items-center">{STATUS[a.status]}</span>
        <span className="text-[13px] font-semibold text-text truncate min-w-0" title={i18nT('pages.chat.activityViewer.subagent', { label: statusLabel })}>{statusLabel}</span>
        {a.agent && <code className="text-[11px] text-muted/50 bg-bg-hover px-1.5 py-0.5 rounded shrink-[3] min-w-0 max-w-[6.5rem] truncate inline-block align-middle" title={a.agent}>{a.agent}</code>}
        {!isPending && <span className="text-[11px] text-muted/40 ml-auto font-mono shrink-0 whitespace-nowrap tabular-nums">{fmtElapsed}</span>}
        {isRunning && <button data-testid="subagent-cancel-btn" className="text-[11px] px-1.5 py-0.5 rounded border border-danger/40 text-danger/70 hover:bg-danger-subtle hover:text-danger cursor-pointer transition-all shrink-0 whitespace-nowrap inline-flex items-center" onClick={onCancel}><X className="lucide-inline" /> {i18nT('pages.chat.activityViewer.cancel')}</button>}
        {isDone && <span className="text-[14px] text-muted bg-bg-hover px-1.5 py-0.5 rounded shrink-0 ml-1">{collapsed ? '▸' : '▾'}</span>}
      </div>
      {/* Input (task) */}
      {!collapsed && (
        <div className="px-3 pt-1 pb-2">
          <div className="text-[10px] text-muted/40 uppercase tracking-wider mb-1">{i18nT('pages.chat.activityViewer.input')}</div>
          <pre className="px-2.5 py-2 bg-bg rounded-md text-[12px] font-mono whitespace-pre-wrap break-all max-h-[120px] overflow-y-auto text-muted/80 leading-relaxed">{a.task}</pre>
        </div>
      )}
      {/* Approval buttons for pending */}
      {isPending && !a.approving && (
        <div className="px-3 pb-2 flex gap-1.5">
          <button className="px-2.5 py-1 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all" onClick={e => onApprove(e, 'approve')}><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.activityViewer.approve')}</button>
          <button className="px-2.5 py-1 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-danger hover:border-danger transition-all" onClick={e => onApprove(e, 'reject')}><Ban className="lucide-inline" /> {i18nT('pages.chat.activityViewer.reject')}</button>
        </div>
      )}
      {isPending && a.approving && <div className="px-3 pb-2 text-[12px] text-muted/50">{i18nT('pages.chat.activityViewer.resolving')}</div>}
      {/* Output (streaming body) */}
      {!isPending && !collapsed && (
      <>
      <div className="px-3 pb-2">
        <div className="text-[10px] text-muted/40 uppercase tracking-wider mb-1">{i18nT('pages.chat.activityViewer.output')}</div>
        <pre ref={bodyRef} onScroll={onScroll} className="px-2.5 py-2 bg-bg rounded-md text-[12px] font-mono whitespace-pre-wrap break-all max-h-[240px] overflow-y-auto text-muted/80 leading-relaxed">
          {a.streaming || a.result || (isDone ? (isNative ? <span className="text-muted/30 italic">{i18nT('pages.chat.activityViewer.output_shown_in_chat')}</span> : <DiskLoader id={a.id} autoLoad={selected} />) : <span className="text-muted/30 italic">{i18nT('pages.chat.activityViewer.waiting_for_output')}</span>)}
          {a.lastTool && <div className="text-accent mt-1"><Wrench className="lucide-inline" /> {a.lastTool}</div>}
        </pre>
      </div>
      {/* Error details */}
      {a.error && (
        <div className="px-3 py-1.5 text-[12px] border-t border-border/20 space-y-0.5">
          <div className="text-red-400">{a.error}</div>
          {a.lastTool && <div className="text-muted/40">{i18nT('pages.chat.activityViewer.last_tool')} {a.lastTool}</div>}
        </div>
      )}
      </>
      )}
    </Clickable>
    </div>
  )
}

/* ── Tool entries are now rendered inline inside chat messages (see ToolCallLine.tsx).
 *    The activity viewer only hosts subagents, logs, and the file browser. ── */

const isSpawnApproval = (e: ToolActivity) => (e.type === 'approval' || e.type === 'approval_resolved') && e.approval_type != null && e.approval_type !== 'chat'

/* ── Approval entry ── */

function ApprovalEntry({ entry, slot }: { entry: ToolActivity; slot: string }) {
  const resolved = entry.type === 'approval_resolved'
  const [localDecision, setLocalDecision] = useState<string | null>(null)
  const isResolved = resolved || !!localDecision
  const [acting, setActing] = useState(false)
  const onAction = useCallback(async (action: string, pattern?: string) => {
    setActing(true)
    setLocalDecision(action)
    try {
      if (entry.approval_type === 'chat') {
        const extra: Record<string, string> = {}
        if (entry.approval_id) extra.request_id = entry.approval_id
        if (pattern) extra.pattern = pattern
        await api.approveChatSlot(slot, action, extra)
      } else {
        await api.resolveApproval(entry.approval_id!, action === 'rejected' ? 'reject' : 'approve')
      }
    } catch { setLocalDecision(null); setActing(false) }
  }, [entry.approval_id, entry.approval_type, slot])

  const toolTitle = entry.text || ''
  const isShell = toolTitle.startsWith('Running: ')
  const normalized = toolTitle.replace(/^(Running: |Reading )/, '')
  const baseCmd = normalized.split(/\s+/)[0] || normalized

  const decisionLabel: Record<string, ReactNode> = { approved: <><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.activityViewer.approved')}</>, trust: <><Handshake className="lucide-inline" /> {i18nT('pages.chat.activityViewer.trusted')}</>, trust_command: <><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.activityViewer.trusted_command')}</>, trust_base: <><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.activityViewer.trusted_base')}</>, rejected: <><Ban className="lucide-inline" /> {i18nT('pages.chat.activityViewer.rejected')}</> }
  const btnClass = 'px-2.5 py-1 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-text hover:border-border-strong hover:bg-bg-hover transition-all'
  return (
    <div className={`mx-2 mb-2 rounded-lg border overflow-hidden shadow-sm transition-all ${isResolved ? 'border-ok/40 bg-card' : 'border-warn/40 bg-warn/5'}`}>
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="shrink-0 flex items-center">{isResolved ? <CheckCircle size={15} className="text-green-400" /> : <Lock size={15} className="text-muted" />}</span>
        <span className="text-[13px] font-semibold text-text truncate min-w-0">{isResolved ? (decisionLabel[localDecision || ''] || i18nT('pages.chat.activityViewer.resolved')) : i18nT('pages.chat.activityViewer.approval_needed')}</span>
        <span className="text-[11px] text-muted/40 font-mono ml-auto shrink-0">{fmtTime(entry.ts)}</span>
      </div>
      {!isResolved && <div className="px-3 pb-2 text-[13px] text-muted/70">{entry.text}</div>}
      {!isResolved && !acting && (
        <div className="px-3 pb-2 flex gap-1.5">
          <button className={btnClass} onClick={() => onAction('approved')}><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.activityViewer.approve')}</button>
          <TrustDropdown
            fullCommand={normalized}
            baseCommand={baseCmd}
            isShell={isShell}
            className={btnClass}
            onAction={(action, pattern) => onAction(action, pattern)}
          />
          <button className={btnClass + ' hover:!text-danger hover:!border-danger'} onClick={() => onAction('rejected')}><Ban className="lucide-inline" /> {i18nT('pages.chat.activityViewer.reject')}</button>
        </div>
      )}
      {acting && <div className="px-3 pb-2 text-[12px] text-muted/50">{i18nT('pages.chat.activityViewer.resolving')}</div>}
    </div>
  )
}

/* ── Links tab ────────────────────────────────────────────────────────────────
 * One scannable list of the links this session surfaced, with a search box that
 * filters by label and URL. Files are NOT listed here: the pinned Files tab
 * browses the project tree and its git working-tree status, which is the
 * general "what changed" view — this tab is only the URLs the conversation
 * referenced. */
function LinksTab({
  sources, issues, navLinks, navResolving,
}: {
  sources?: PullRequestLink[]
  issues?: PullRequestLink[]
  navLinks?: ExtractedLink[]
  navResolving?: boolean
}) {
  const [query, setQuery] = useState('')

  // Hide links that already have a RICH panel of their own — the Changes tab's
  // `sources` and the Issues tab's `issues`. Keep every other link, including
  // cr-classified hosts (Bitbucket, self-hosted, code reviews) and
  // non-allowlisted issue hosts that neither parser can render, so they stay
  // reachable here instead of vanishing from the panel.
  const richUrls = new Set([...(sources || []), ...(issues || [])].map(s => resourceKey(s.url)))
  const resourceLinks = dedupResourceLinks((navLinks || []).filter(l => !richUrls.has(resourceKey(l.url))))

  const q = query.trim().toLowerCase()
  const filteredLinks = q
    ? resourceLinks.filter(l => (l.label || '').toLowerCase().includes(q) || l.url.toLowerCase().includes(q))
    : resourceLinks

  const isEmpty = resourceLinks.length === 0
  const noMatches = !isEmpty && filteredLinks.length === 0
  // Only offer the search box once the list is long enough that scanning it by
  // eye stops being the faster option — a short list needs no filter. The
  // `query` clause matters: the box must stay mounted while a query is active,
  // or a filter that shrinks the list below the threshold would unmount its own
  // input and keep filtering invisibly, with no way to clear it.
  const showSearch = resourceLinks.length > 5 || query !== ''
  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {showSearch && (
        <div className="px-3 pt-2 pb-0.5 shrink-0">
          <div className="relative">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted/60 pointer-events-none" />
            <input
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={i18nT('pages.chat.activityViewer.search_files')}
              className="w-full h-7 pl-8 pr-8 rounded-md bg-bg-elevated border border-border text-[12px] text-text placeholder:text-muted/50 focus:outline-none focus-visible:border-border-strong transition-colors"
              aria-label={i18nT('pages.chat.activityViewer.search_files')}
            />
            {query && (
              <button
                onClick={() => setQuery('')}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 p-1 rounded text-muted/50 hover:text-text transition-colors bg-transparent border-none cursor-pointer"
                title={i18nT('pages.chat.activityViewer.clear')}
                aria-label={i18nT('pages.chat.activityViewer.clear')}
              >
                <X size={12} />
              </button>
            )}
          </div>
        </div>
      )}
      <div className="flex-1 overflow-y-auto py-1.5">
        {isEmpty ? (
          <div className="flex-1 flex items-center justify-center text-muted text-[13px] py-8">{i18nT('pages.chat.activityViewer.no_links_yet')}</div>
        ) : noMatches ? (
          <div className="flex-1 flex items-center justify-center text-muted text-[13px] py-8">{i18nT('pages.chat.activityViewer.no_matches')}</div>
        ) : (
          <div className="px-3 mb-2">
            <PanelSectionHeader
              label={i18nT('pages.chat.activityViewer.resources')}
              count={filteredLinks.length}
              className="mt-1 mb-0.5"
              trailing={navResolving
                ? <span className="text-[10px] text-accent animate-pulse">{i18nT('pages.chat.activityViewer.resolving_2')}</span>
                : undefined}
            />
            <div className="flex flex-col">
              {filteredLinks.map((link, i) => (
                <ResourceRow key={i} link={link} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// Re-exported so the symbol `ActivityViewer` exported before this extraction
// stays importable from here; the implementation lives in `utils/diffLineCounts`
// so a pure test need not pull this page's module graph.
export { countDiffStats }

/* ── Main component ── */

/* ── Resource-link list row ──────────────────────────────────────────────────
 * One row of the Links tab, left to right: a fixed-width type icon (pull-request
 * glyph in accent for code reviews, a filled dot in ok for provider issues, link
 * glyph in muted otherwise), the link label, the host as a dimmed subtitle, and
 * a trailing external-link arrow in the row's right slot. The whole row is the
 * anchor, so any part of it opens the link in a new tab. */
function ResourceRow({ link }: { link: ExtractedLink }) {
  const { Icon, colorCls } = link.type === 'cr'
    ? { Icon: GitPullRequest, colorCls: 'text-accent' }
    : link.type === 'issue'
      ? { Icon: CircleDot, colorCls: 'text-ok' }
      : { Icon: LinkIcon, colorCls: 'text-muted' }
  const typeLabel = resourceTypeLabel(link.type)
  let host = ''
  try { host = new URL(link.url).hostname.replace(/^www\./, '') } catch { host = link.url }
  return (
    <a
      href={link.url}
      target="_blank"
      rel="noopener noreferrer"
      className="group flex items-center gap-2 px-2 py-1 rounded-md hover:bg-bg-hover transition-colors no-underline"
      title={link.url}
    >
      <Icon size={14} className={`shrink-0 ${colorCls}`} aria-hidden="true" />
      {/* The icon carries the type VISUALLY; this keeps a text alternative so
       *  the type is not conveyed by shape/colour alone (it would otherwise be
       *  invisible to a screen reader). sr-only costs no layout, so it does not
       *  shift the label off the row's left text edge. */}
      <span className="sr-only">{typeLabel}</span>
      <span className="min-w-0 flex-1 flex flex-col leading-tight">
        <span className="text-[12.5px] text-text truncate">{link.label}</span>
        {host && <span className="text-[10.5px] text-muted/80 truncate">{host}</span>}
      </span>
      <ExternalLink size={12} className="shrink-0 text-muted/40 group-hover:text-muted transition-colors" />
    </a>
  )
}

/* ── SessionArtifactsTab ─────────────────────────────────────────────────────
 *
 * Two sections, so the tab is both a session view AND a library browser:
 *
 *  A. "This session" — everything this session was involved with, from TWO
 *     inputs:
 *       1. Artifacts scoped by `?touched_by=` — the session's *involvement*
 *          scope, not just its output: artifacts it created, read, edited,
 *          iterated on or reverted (the backend unions the create-time
 *          `session_key` with every event's `session_id`). Includes each
 *          `<mcwidget>` the agent emitted, which the backend auto-registers
 *          unpinned (kiro_crew/widget_artifacts.py). These have no filesystem
 *          path — a widget's HTML lives inline in the message.
 *       2. The session's bound companion artifact, if any. A session started
 *          from an artifact's detail page carries `slot.artifact`, persisted in
 *          the history meta line — so the binding still resolves after the user
 *          leaves the detail page, picks the session up on the main chat page,
 *          and opens this tab. Listed even when the agent never touched the
 *          artifact, because the binding itself is the association.
 *
 *  B. "From your library" — a search field that pulls a SPECIFIC prior artifact
 *     into this session (results de-duped against section A), plus a link to the
 *     full /artifacts page. This replaces the old inline library mirror: the
 *     panel stays scoped to the conversation, and the /artifacts page remains
 *     the home for browsing the whole library.
 *
 * Every row is a real artifact RECORD. This tab used to also list "session
 * documents" — plain files the agent wrote, admitted purely on their extension
 * (`.md`/`.txt`/`.rst`/…) — which meant any scratch note appeared here as if it
 * were an artifact. Files belong to the Files tab; the library is curated, so
 * getting into it is an explicit act. Plain-file rows are gone, and with them the
 * doc↔artifact-twin reconciliation the two overlapping inputs required.
 */
type SessionArtifactRow = {
  key: string
  name: string
  sub: string
  slug: string
  /** True only for a chat-emitted widget the store auto-registered and that is
   *  still unpinned — the one state where the row offers "save permanently".
   *  See `savePermanently` on the row component for why nothing else does. */
  offerSave: boolean
}

/** Cap on library search results shown inline — the panel is a ~460px rail, so
 *  a search that matches half the library still shows a readable slice, and the
 *  "Browse all" link goes to the full /artifacts page for the rest. */
const LIBRARY_SEARCH_CAP = 20

/** Project one artifact record onto a row. Single mapper so a section-A row and
 *  its section-B twin can never disagree about what the row offers. */
const toRow = (a: Artifact): SessionArtifactRow => ({
  key: `artifact:${a.slug}`,
  name: a.name || a.slug,
  sub: a.kind,
  slug: a.slug,
  offerSave: !!a.auto_registered && !a.pinned,
})

function SessionArtifactsTab({ slot, onArtifactOpen }: { slot: string; onArtifactOpen?: (slug: string) => void }) {
  const qc = useQueryClient()
  // Artifact rows have no filesystem path, so the file-open path can't
  // serve them; `onArtifactOpen` is their twin and opens an artifact tab in this
  // same panel. It is optional because this tab also renders outside a chat (no
  // panel to open into), where the standalone detail page stays the target.
  const navigate = useNavigate()
  const { data: artifactData, isFetching: artifactsFetching } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['session-artifact-records', slot],
    queryFn: () => api.artifacts({ touchedBy: slot }),
    enabled: !!slot,
  })
  // The whole library, for section B. Its own query key so the session query's
  // invalidations don't force a refetch of the (larger) library list and vice
  // versa; both still refresh on the shared ['artifacts'] invalidation below.
  const { data: libraryData, isFetching: libraryFetching } = useQuery<{ artifacts: Artifact[] }>({
    queryKey: ['artifacts', 'panel-library'],
    queryFn: () => api.artifacts({}),
  })
  // Companion binding for this slot. Narrowed to the primitive so an unrelated
  // slot field changing (message count, running flag) can't re-render the tab.
  const boundArtifactSlug = useAppSelector(
    s => s.dashboard.slots.find(x => x.key === slot)?.artifact || '',
  )
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['session-artifact-records', slot] })
    qc.invalidateQueries({ queryKey: ['artifacts'] })
  }
  const pinMut = useMutation({
    // Real session key, not the transport's shared placeholder: this pin is made
    // on behalf of THIS chat slot, so a restricted (incognito) slot must be gated.
    mutationFn: (slug: string) =>
      api.setArtifactPinned(slug, true, slot ? `dashboard:${slot}` : undefined),
    onSuccess: invalidate,
  })
  const busySlug = pinMut.isPending ? (pinMut.variables as string) : null

  const rows = useMemo<SessionArtifactRow[]>(() => {
    const out: SessionArtifactRow[] = (artifactData?.artifacts || []).map(toRow)
    // The bound companion artifact belongs in this section even if the agent
    // never touched it, so it is added when the touched_by scan didn't already
    // return it. Its metadata comes from the library list rather than a second
    // query; if the library hasn't loaded (or the artifact was deleted while the
    // binding lingers) there is nothing to render, so skip it rather than
    // inventing a placeholder row.
    if (boundArtifactSlug && !out.some(r => r.slug === boundArtifactSlug)) {
      const bound = (libraryData?.artifacts || []).find(a => a.slug === boundArtifactSlug)
      if (bound) out.unshift(toRow(bound))
    }
    return out
  }, [artifactData, boundArtifactSlug, libraryData])

  // Section B: the library minus whatever section A already shows. Both sides
  // are artifact records now, so slug is the whole join — the extra
  // `source_path` join this used to carry existed only to reconcile a
  // file-backed artifact against its plain-file twin row, and there are no
  // plain-file rows left to reconcile against.
  const libraryRows = useMemo<SessionArtifactRow[]>(() => {
    const shownSlugs = new Set(rows.map(r => r.slug).filter(Boolean))
    return (libraryData?.artifacts || [])
      .filter(a => !shownSlugs.has(a.slug))
      .map(a => ({ ...toRow(a), key: `lib:${a.slug}` }))
  }, [libraryData, rows])

  const loading = artifactsFetching
  const libraryTotal = libraryData?.artifacts?.length ?? 0
  // Search the library (section-A items already excluded) as a pull-in
  // affordance. Results appear ONLY while a query is present — an empty query
  // never dumps the whole library inline; that is what the /artifacts page is for.
  const [libQuery, setLibQuery] = useState('')
  const filteredLibrary = useMemo<SessionArtifactRow[]>(() => {
    const q = libQuery.trim().toLowerCase()
    if (!q) return []
    return libraryRows.filter(r => r.name.toLowerCase().includes(q)).slice(0, LIBRARY_SEARCH_CAP)
  }, [libQuery, libraryRows])

  const openRow = useCallback((r: SessionArtifactRow) => {
    if (!r.slug) return
    // Panel tab when a host provided one (the chat case); otherwise fall back
    // to the standalone page so this row is never a dead click.
    if (onArtifactOpen) onArtifactOpen(r.slug)
    else navigate(`/artifacts/${r.slug}`)
  }, [onArtifactOpen, navigate])
  const savePermanently = useCallback((r: SessionArtifactRow) => {
    if (r.slug) pinMut.mutate(r.slug)
  }, [pinMut])
  const rowBusy = useCallback(
    (r: SessionArtifactRow) => !!r.slug && busySlug === r.slug,
    [busySlug],
  )

  // Still loading with nothing resolved yet — show a single spinner line rather
  // than flashing the empty hero before data lands.
  if ((loading || libraryFetching) && rows.length === 0 && libraryTotal === 0) {
    return (
      <div className="flex-1 overflow-y-auto py-1.5">
        <div className="flex-1 flex items-center justify-center text-muted text-[13px] py-8">
          {i18nT('pages.chat.activityViewer.loading')}
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto py-1.5">
      <div className="px-3 flex flex-col">
        {/* Section A — this session. When the session has touched nothing yet, a
            short hero explains what the panel collects instead of an empty heading. */}
        {rows.length > 0 ? (
          <>
            <PanelSectionHeader
              label={i18nT('pages.chat.activityViewer.artifacts_this_session')}
              count={rows.length}
              className="mt-0.5 mb-0.5"
            />
            {rows.map(r => (
              <ArtifactListRow key={r.key} row={r} busy={rowBusy(r)} onOpen={openRow} onSave={savePermanently} />
            ))}
          </>
        ) : (
          <div className="flex flex-col items-center text-center px-4 pt-6 pb-1">
            <Component size={22} className="text-muted/50" />
            <div className="mt-2.5 text-[13px] font-medium text-text">
              {i18nT('pages.chat.activityViewer.artifacts_empty_title')}
            </div>
            <div className="mt-1 text-[12px] text-muted leading-snug max-w-[260px]">
              {i18nT('pages.chat.activityViewer.artifacts_empty_hint')}
            </div>
          </div>
        )}
        {/* Section B — bridge to the wider library: search to pull a specific
            artifact into this session, plus a link to the full /artifacts page.
            Replaces the old inline library mirror. */}
        {libraryTotal > 0 && (
          <div className={rows.length > 0 ? 'mt-3' : 'mt-4'}>
            <PanelSectionHeader
              label={i18nT('pages.chat.activityViewer.artifacts_from_library')}
              className="mb-1.5"
            />
            <div className="relative">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
              <input
                type="text"
                value={libQuery}
                onChange={e => setLibQuery(e.target.value)}
                placeholder={i18nT('pages.chat.activityViewer.artifacts_search_library')}
                aria-label={i18nT('pages.chat.activityViewer.artifacts_search_library')}
                className="w-full text-[12px] pl-7 pr-2.5 py-1.5 rounded-md bg-bg border border-border text-text placeholder:text-muted focus:outline-none focus-visible:border-accent transition-colors"
              />
            </div>
            {libQuery.trim() && (
              filteredLibrary.length > 0 ? (
                <div className="mt-1">
                  {filteredLibrary.map(r => (
                    <ArtifactListRow key={r.key} row={r} busy={rowBusy(r)} onOpen={openRow} onSave={savePermanently} />
                  ))}
                </div>
              ) : (
                <div className="mt-2 px-2 text-[11.5px] text-muted">
                  {i18nT('pages.chat.activityViewer.no_matches')}
                </div>
              )
            )}
            <button
              type="button"
              onClick={() => navigate('/artifacts')}
              className="self-start mt-1.5 px-2 py-1 text-[11.5px] text-accent hover:underline bg-transparent border-none cursor-pointer transition-colors"
            >
              {i18nT('pages.chat.activityViewer.artifacts_browse_all', { count: libraryTotal })}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

/** One row of the Artifacts tab — always a real artifact record.
 *  Module-scope (not nested in SessionArtifactsTab): a nested definition would
 *  be a new component type on every render, remounting every row and dropping
 *  the save button's pending state mid-flight. */
function ArtifactListRow({ row, busy, onOpen, onSave }: {
  row: SessionArtifactRow
  busy: boolean
  onOpen: (row: SessionArtifactRow) => void
  onSave: (row: SessionArtifactRow) => void
}) {
  return (
    <div className="flex items-center gap-2 px-2 py-1 rounded-md hover:bg-bg-hover transition-colors">
      <button
        type="button"
        onClick={() => onOpen(row)}
        className="flex items-center gap-2 min-w-0 flex-1 text-left bg-transparent border-none cursor-pointer p-0"
        title={i18nT('pages.chat.activityViewer.open_artifact')}
      >
        {/* Identity glyph. Deliberately NOT reused as the action icon on the
            right: two of the same glyph in one row, only one of them clickable,
            reads as a rendering bug. */}
        <Component size={14} className="text-accent shrink-0" />
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block text-[12.5px] text-text truncate">{row.name}</span>
          <span className="block text-[10.5px] text-muted/80 truncate">{row.sub}</span>
        </span>
      </button>
      {/* "Save permanently", and ONLY for a chat-emitted widget that is still
          unpinned. That is the single case where the flag changes an outcome:
          the store sweeps auto-registered widgets oldest-first past
          MAX_AUTO_WIDGET_ARTIFACTS (200) unless they are pinned
          (kiro_crew/artifacts.py — prune_auto_widgets). For an explicitly
          created artifact nothing sweeps it, so the same control would promise
          safety it isn't providing. One-way by design: there is no un-save
          affordance here, because the only thing un-saving buys is
          eligibility for deletion. */}
      {row.offerSave && (
        <button
          type="button"
          disabled={busy}
          data-testid={`artifact-save-${row.slug}`}
          onClick={() => onSave(row)}
          className="shrink-0 p-1 rounded transition-colors bg-transparent border-none cursor-pointer disabled:cursor-default text-muted/50 hover:text-accent"
          title={i18nT('pages.chat.activityViewer.artifact_save_permanently')}
          aria-label={i18nT('pages.chat.activityViewer.artifact_save_permanently_aria', { name: row.name })}
        >
          {busy ? <LoaderIcon size={13} className="animate-spin" /> : <BookmarkPlus size={13} />}
        </button>
      )}
    </div>
  )
}

export default function ActivityViewer({ subagents, toolLog, open, onToggle, slot, onFileOpen, onArtifactOpen, navLinks, navResolving, view, sources, selectedSourceUrl, onSelectSource, onReconcileSource, issues, selectedIssueUrl, onSelectIssue, onReconcileIssue, onAddToChat, pins, pinsLoading, onJumpToPin, onUnpin, slotTitle, chatMode, projectDir }: {
  subagents: Record<string, SubagentActivity>; toolLog: ToolActivity[]; open: boolean; onToggle: () => void; slot: string
  onFileOpen?: (path: string) => void; onArtifactOpen?: (slug: string) => void
  projectDir?: string
  navLinks?: ExtractedLink[]; navResolving?: boolean
  sources?: PullRequestLink[]; selectedSourceUrl?: string; onSelectSource?: (url: string) => void; onReconcileSource?: (url: string) => void; onAddToChat?: (text: string) => void
  /** Issue links mentioned in this session, plus the Issues tab's own selection. */
  issues?: PullRequestLink[]; selectedIssueUrl?: string; onSelectIssue?: (url: string) => void; onReconcileIssue?: (url: string) => void
  /** Pinned messages for this session (Pins tab). Passed down rather than
   *  queried here so the jump stays with ChatPage, which owns the transcript
   *  and can page older history in when the target is out of the loaded window.
   *  `slotTitle` / `chatMode` only shape the copyable deep link. */
  pins?: ChatPin[]
  pinsLoading?: boolean
  onJumpToPin?: (messageTs: string, mid?: string) => void
  onUnpin?: (id: string) => void
  slotTitle?: string
  chatMode?: string
  /** When set, render ONLY this view and hide the internal SegmentedControl.
   *  Used by SidePanel, which owns the top-level tab strip. */
  view?: 'changes' | 'issues' | 'subagents' | 'logs' | 'context' | 'links' | 'artifacts' | 'side' | 'workflows' | 'git' | 'summary' | 'pins'
}) {
  const dispatch = useAppDispatch()
  const [, setSelected] = useState(0)
  const reduxTab = useAppSelector(s => s.chat.activityTab)
  const [tab, setTab] = useState<'changes' | 'issues' | 'subagents' | 'workflows' | 'logs' | 'links' | 'side' | 'artifacts'>(reduxTab === ('nav' as string) ? 'changes' : reduxTab)
  const hasSources = (sources?.length || 0) > 0
  const hasIssues = (issues?.length || 0) > 0
  const explicitTab = useRef(false)
  const containerRef = useRef<HTMLDivElement>(null)
  // Exception-first ordering: agents needing attention (failed, stalled,
  // retrying, pending approval) sort to the top; the healthy/finished
  // majority follows. Stable within a rank (insertion order preserved).
  const ids = useMemo(() => {
    const rank = (a: SubagentActivity | undefined) => {
      if (!a) return 9
      if (a.status === 'error') return 0
      if (a.retrying) return 1
      if (a.stalled) return 2
      if (a.status === 'pending') return 3
      if (a.status === 'running' || a.status === 'tool') return 4
      if (a.status === 'stopped') return 5
      return 6 // done
    }
    return Object.keys(subagents).sort((x, y) => rank(subagents[x]) - rank(subagents[y]))
  }, [subagents])
  const hasSubagents = ids.length > 0
  // Agents accepted but not yet started — queued behind the concurrency cap /
  // stagger gate, so they have no per-agent entry in `subagents` yet. Without
  // this the panel renders "No subagents running" during the entire ramp of a
  // freshly-accepted wave, which is flatly false and the single most confusing
  // state this panel had.
  const queuedCount = useAppSelector(s => s.chat.subagentQueued?.[slot] ?? 0)
  // Render cap: bounds DOM at 60-100 agents; exceptions are always within
  // the cap thanks to the ordering above.
  const [showAllSubagents, setShowAllSubagents] = useState(false)
  const visibleIds = showAllSubagents ? ids : ids.slice(0, 30)
  const cappedCount = ids.length - visibleIds.length
  // 1-click transcript: a chip row click selects an agent — ensure it is
  // rendered (even past the cap), scrolled to, expanded, and disk-loaded.
  const selectedSubagentId = useAppSelector(s => s.chat.selectedSubagentId)
  const dispatchRedux = useAppDispatch()
  useEffect(() => {
    if (selectedSubagentId && !visibleIds.includes(selectedSubagentId) && ids.includes(selectedSubagentId)) {
      setShowAllSubagents(true)
    }
  }, [selectedSubagentId, visibleIds, ids])
  const terminalIds = useMemo(
    () => ids.filter(id => ['done', 'error', 'stopped'].includes(subagents[id]?.status ?? '')),
    [ids, subagents],
  )
  const failedRetryableIds = useMemo(
    () => ids.filter(id => subagents[id]?.status === 'error' && !id.startsWith('native:')),
    [ids, subagents],
  )
  const [retryingFailed, setRetryingFailed] = useState(false)
  const retryFailed = useCallback(() => {
    setRetryingFailed(true)
    Promise.allSettled(failedRetryableIds.map(id => api.spawnRetry(id))).finally(() => setRetryingFailed(false))
  }, [failedRetryableIds])
  const dismissDone = useCallback(() => {
    // Slot-scoped by construction: delete exactly this slot's terminal cards
    // by id — the global DELETE /api/spawn clear would nuke other sessions'
    // completed agents too (their cards would 404 on status/output).
    for (const id of terminalIds) api.spawnDelete(id).catch(() => {})
    dispatchRedux(clearTerminalSubagents({ slot }))
  }, [dispatchRedux, slot, terminalIds])

  // Dynamic Workflow runs (M6) — dedup + caching + self-managed polling
  const { data: wfRuns = [] } = useQuery<WfRunRow[]>({
    queryKey: ['workflow-runs'],
    queryFn: () =>
      fetch('/api/workflows/runs', { credentials: 'same-origin' })
        .then(r => (r.ok ? r.json() : { runs: [] }))
        .then(d => (Array.isArray(d?.runs) ? d.runs : [])),
    enabled: open,
    refetchInterval: 2500,
  })
  const wfRunsForSlot = wfRuns.filter(r => runBelongsToSlot(r.session_key, slot))
  const wfRunningCount = wfRunsForSlot.filter(r => r.status === 'running').length

  const visibleLog = toolLog.filter(e => e.type !== 'reasoning')

  // Subagent events are subscribed eagerly at WS connect time — no need to toggle here.

  useEffect(() => { setTab(reduxTab === ('nav' as string) ? 'changes' : reduxTab); explicitTab.current = true }, [reduxTab])

  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault(); onToggle()
    }
    const el = containerRef.current
    el?.addEventListener('keydown', handler)
    return () => el?.removeEventListener('keydown', handler)
  }, [open, onToggle])

  // Auto-switch to subagents tab when subagents or spawn approvals first appear
  const hadSubagents = useRef(false)
  const hasSpawnApprovals = visibleLog.some(e => e.type === 'approval' && isSpawnApproval(e))
  const hasSubagentActivity = hasSubagents || hasSpawnApprovals
  useEffect(() => {
    if (hasSubagentActivity && !hadSubagents.current && !explicitTab.current) setTab('subagents')
    hadSubagents.current = hasSubagentActivity
    explicitTab.current = false
  }, [hasSubagentActivity])

  if (!open) return null

  // When a `view` prop is supplied, SidePanel owns the tab strip — render only
  // that view and skip the internal SegmentedControl.
  const requestedTab = view ?? tab
  // The internal SegmentedControl lists a Changes segment only when there are
  // sources, so a `changes` selection with none left falls back to Links — the
  // segment that is always in the strip. The `!view` guard confines that to the
  // internal strip: under `view` mode SidePanel owns the strip and Changes is a
  // PINNED tab that is always present, so it stays on `changes` and renders its
  // own PR empty state, mirroring how the (unpinned) Issues view owns its empty
  // state. Every call site in the app passes `view`, so the internal strip — and
  // this fallback with it — is reachable only without one.
  const effectiveTab = requestedTab === 'changes' && !hasSources && !view ? 'links' : requestedTab

  const TABS: { key: typeof tab; label: string; icon: ReactNode; count?: number }[] = [
    ...(hasSources ? [{ key: 'changes' as const, label: i18nT('pages.chat.activityViewer.changes'), icon: <GitPullRequest size={13} />, count: sources!.length }] : []),
    ...(hasIssues ? [{ key: 'issues' as const, label: i18nT('pages.chat.activityViewer.issues'), icon: <CircleDot size={13} />, count: issues!.length }] : []),
    { key: 'links', label: i18nT('pages.chat.activityViewer.links'), icon: <LinkIcon size={13} />, count: navLinks?.length || 0 },
    { key: 'artifacts', label: i18nT('pages.chat.activityViewer.artifacts'), icon: <Component size={13} /> },
    { key: 'subagents', label: i18nT('pages.chat.activityViewer.subagents'), icon: <Bot size={13} />, count: ids.length + visibleLog.filter(isSpawnApproval).length },
    { key: 'workflows', label: i18nT('pages.chat.activityViewer.workflows'), icon: <Workflow size={13} />, count: wfRunningCount },
    { key: 'logs', label: i18nT('pages.chat.activityViewer.logs'), icon: <ScrollText size={13} /> },
    { key: 'side', label: i18nT('pages.chat.activityViewer.side'), icon: <MessageCircleQuestionMark size={13} /> },
  ]

  return (
    // Focusable container so the imperative Escape keydown listener (attached to
    // containerRef in the effect above) has a focus target; the panel itself is
    // a region, not an interactive control.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <div ref={containerRef} role="region" aria-label={i18nT('pages.chat.activityViewer.activity')} className="flex flex-col h-full bg-bg relative" tabIndex={0}>
      {/* Tab bar — hidden when SidePanel drives the view via the `view` prop. */}
      {!view && (
        <div className="px-3 py-2 shrink-0 flex justify-center">
          <SegmentedControl
            segments={TABS}
            value={effectiveTab === 'context' || effectiveTab === 'git' || effectiveTab === 'summary' || effectiveTab === 'pins' ? tab : effectiveTab}
            onChange={t => { setTab(t); explicitTab.current = true; dispatch(openActivityToTab(t)) }}
            layoutId="activity-tab"
          />
        </div>
      )}

      {/* Changes (pull request sources) view */}
      {effectiveTab === 'changes' && (
        <div className="flex-1 min-h-0 overflow-hidden">
          {hasSources ? (
            <PullRequestPanel
              sources={sources!}
              selectedUrl={selectedSourceUrl || ''}
              onSelect={onSelectSource || (() => {})}
              onReconcile={onReconcileSource}
              onAddToChat={onAddToChat || (() => {})}
            />
          ) : (
            <div className="text-muted text-[13px] pt-8 px-6 text-center">
              {i18nT('pages.chat.activityViewer.no_pull_requests_yet')}
            </div>
          )}
        </div>
      )}

      {/* Git (project working tree + history) view */}
      {effectiveTab === ('git' as string) && (
        <div className="flex-1 min-h-0 overflow-hidden">
          {projectDir ? (
            <GitPanel projectDir={projectDir} onFileOpen={onFileOpen} onClose={onToggle} />
          ) : (
            <div className="text-muted text-[13px] pt-8 px-6 text-center">
              {i18nT('components.gitPanel.no_project')}
            </div>
          )}
        </div>
      )}

      {/* Issues (issue sources) view */}
      {effectiveTab === 'issues' && (
        <div className="flex-1 min-h-0 overflow-hidden">
          {hasIssues ? (
            <IssuePanel
              issues={issues!}
              selectedUrl={selectedIssueUrl || ''}
              onSelect={onSelectIssue || (() => {})}
              onReconcile={onReconcileIssue}
              onAddToChat={onAddToChat}
            />
          ) : (
            <div className="flex-1 flex items-center justify-center h-full text-muted text-[13px] py-8 px-6 text-center">
              {i18nT('pages.chat.activityViewer.no_issues_in_this_session_yet_mention_a_github_o')}
            </div>
          )}
        </div>
      )}

      {/* Subagents tab */}
      {effectiveTab === 'subagents' && (
        <div className="flex-1 overflow-y-auto py-2">
          {/* Batch controls (scale): retry failures, clear the finished pile */}
          {(failedRetryableIds.length > 0 || terminalIds.length > 0) && (
            <div className="mx-2 mb-2 flex flex-wrap items-center gap-1.5">
              {failedRetryableIds.length > 0 && (
                <button
                  className="flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-accent/40 text-accent/80 hover:bg-accent/10 hover:text-accent cursor-pointer transition-all bg-transparent disabled:opacity-50 shrink-0 whitespace-nowrap"
                  onClick={retryFailed}
                  disabled={retryingFailed}
                  data-testid="retry-failed-btn"
                >
                  <RotateCcw size={11} className={retryingFailed ? 'animate-spin' : ''} /> {i18nT('pages.chat.activityViewer.retry_failed_count', { count: failedRetryableIds.length })}
                </button>
              )}
              {terminalIds.length > 0 && (
                <button
                  className="flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-border text-muted hover:text-text hover:border-border-strong cursor-pointer transition-all bg-transparent shrink-0 whitespace-nowrap"
                  onClick={dismissDone}
                  data-testid="dismiss-done-btn"
                >
                  <X size={11} /> {i18nT('pages.chat.activityViewer.dismiss_done_count', { count: terminalIds.length })}
                </button>
              )}
            </div>
          )}
          {/* Pending approvals */}
          {visibleLog.filter(isSpawnApproval).map((entry, i) => (
            <ApprovalEntry key={`a${i}`} entry={entry} slot={slot} />
          ))}
          {/* Accepted-but-not-started banner: the only signal for a wave still
              behind the concurrency cap. Shown alongside started agents too,
              since a staggered ramp has both at once. */}
          {queuedCount > 0 && (
            <div
              className="mx-2 mb-2 flex items-center gap-1.5 text-[12px] text-muted rounded border border-dashed border-border px-2 py-1.5"
              data-testid="subagent-queued-banner"
              role="status"
            >
              <Clock size={12} className="shrink-0" aria-hidden />
              <span>
                {queuedCount} {i18nT('pages.chat.activityViewer.waiting_to_start_queued_behind_the_concurrency_l')}
              </span>
            </div>
          )}
          {hasSubagents ? (
            <>
              {visibleIds.map((id, i) => (
                <SubagentPane
                  key={id}
                  a={subagents[id]}
                  slot={slot}
                  onClick={() => setSelected(i)}
                  selected={id === selectedSubagentId}
                />
              ))}
              {cappedCount > 0 && (
                <button
                  className="mx-2 mb-3 w-[calc(100%-16px)] text-[12px] text-muted hover:text-text py-2 rounded border border-dashed border-border cursor-pointer bg-transparent transition-colors"
                  onClick={() => setShowAllSubagents(true)}
                  data-testid="show-all-subagents"
                >
                  {i18nT('pages.chat.activityViewer.show_all_count', { count: ids.length })}
                </button>
              )}
            </>
          ) : visibleLog.filter(isSpawnApproval).length === 0 && queuedCount === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-muted/30 gap-2">
              <span className="text-[24px]"><Bot className="lucide-inline" /></span>
              <span className="text-[13px]">{i18nT('pages.chat.activityViewer.no_subagents_running')}</span>
            </div>
          )}
        </div>
      )}

      {/* Workflows tab (M6): live dynamic-workflow runs */}
      {effectiveTab === 'workflows' && (
        <div className="flex-1 overflow-y-auto py-2 px-3 flex flex-col gap-2">
          {wfRunsForSlot.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-muted/30 gap-2">
              <span className="text-[24px]"><Workflow className="lucide-inline" /></span>
              <span className="text-[13px]">{i18nT('pages.chat.activityViewer.no_workflow_runs')}</span>
              <span className="text-[11px] text-center px-4">
                {i18nT('pages.chat.activityViewer.ask_me_to_use_a_dynamic_workflow_to_runs_from_th')}
              </span>
            </div>
          ) : (
            wfRunsForSlot.map(r => <WorkflowSidebarRow key={r.run_id} row={r} />)
          )}
        </div>
      )}

      {/* Logs tab — LogViewer is an edge-to-edge page component; give it a
          little breathing room inside the panel. */}
      {effectiveTab === 'logs' && (
        <div className="flex-1 min-h-0 flex flex-col px-2 pb-2 pt-1">
          <LogViewer compact />
        </div>
      )}

      {/* Links tab */}
      {effectiveTab === 'links' && (
        <LinksTab
          sources={sources}
          issues={issues}
          navLinks={navLinks}
          navResolving={navResolving}
        />
      )}

      {/* Artifacts tab (in-session documents) */}
      {effectiveTab === 'artifacts' && <SessionArtifactsTab slot={slot} onArtifactOpen={onArtifactOpen} />}

      {/* Side tab */}
      {/* Sits next to Logs on purpose: both answer "what actually happened
          in THIS session" — Logs for the tool calls, this for the context
          that was injected around them. */}
      {effectiveTab === 'context' && <ContextBreakdownTab slot={slot} />}

      {/* Session summary — the goal-level view of this session, so returning to
          it does not mean re-reading the transcript. */}
      {effectiveTab === 'summary' && <SessionSummaryTab key={slot} slot={slot} />}

      {/* Pinned messages — the user's own bookmarks in this transcript. Grouped
          with Summary rather than given its own dock: both are ways back into
          the conversation, and a second right-hand column competing with this
          one is what made the standalone panel hard to place. */}
      {effectiveTab === 'pins' && (
        <div className="flex-1 min-h-0 overflow-hidden">
          <PinnedMessagesPanel
            pins={pins ?? []}
            loading={!!pinsLoading}
            slotKey={slot}
            slotTitle={slotTitle}
            mode={chatMode}
            onJumpToMessage={onJumpToPin ?? (() => {})}
            onUnpin={onUnpin ?? (() => {})}
          />
        </div>
      )}

      {effectiveTab === 'side' && <SideChat slot={slot} />}

      {/* Scroll to bottom button (tools tab only) */}
    </div>
  )
}
