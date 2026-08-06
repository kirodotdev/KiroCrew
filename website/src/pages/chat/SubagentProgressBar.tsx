import { useState, useRef, useEffect, useMemo, useCallback, memo } from 'react'
import { Bot, X, AlertTriangle, Loader2, CheckCircle, AlertCircle, Square, RotateCcw, Clock, ChevronRight } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { openActivityToTab, selectSubagent, sseSubagentDone } from '../../store/chatSlice'
import { api } from '../../api/client'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { SubagentActivity } from '../../types'

import { i18nT } from '../../i18n/t'
const EMPTY_SUBAGENTS: Record<string, SubagentActivity> = {}

/** Max agent rows rendered in the chip — exceptions (stalled/retrying) sort
 *  first, the healthy remainder collapses into a summary row. Bounds chip DOM
 *  at 60-100 concurrent agents without a virtualization dependency. */
const CHIP_MAX_ROWS = 8

/** localStorage key persisting the user's collapse choice for the wave chip.
 *  Default is expanded (matches the long-standing behaviour); the toggle only
 *  adds the ability to shrink the chip to its one-line header when a big wave
 *  would otherwise push the composer down. Choice survives across sessions. */
const COLLAPSE_KEY = 'mc.subagentChip.collapsed'

/** Minimal shape of the `/api/spawn` list response consumed for reconciliation. */
interface SpawnListAgent {
  id: string
  done?: boolean
  parent?: string
}
interface SpawnListResponse {
  agents?: SpawnListAgent[]
}

/** Active subagent summary above the chat input. */
const SubagentProgressBar = memo(function SubagentProgressBar({ slot }: { slot: string | null }) {
  // Use chatSlice.subagents — populated by subagent_spawn/tool/done WS events
  // (dashboardSlice.subagentRunning only updates on subagent_status which fires at completion)
  const dispatch = useAppDispatch()
  const subagents = useAppSelector(s => slot === s.chat.activeSlot ? s.chat.subagents : s.chat.slotActivity[slot ?? '']?.subagents ?? EMPTY_SUBAGENTS)
  // Aggregate "waiting to start" count for this slot — agents accepted but
  // queued behind the concurrency cap / stagger gate (no individual card yet).
  const queued = useAppSelector(s => s.chat.subagentQueued?.[slot ?? ''] ?? 0)
  // Only top-level (managed) subagents belong in the chip — its count must
  // match the "spawned N" prose. Native kiro-cli sub-agents (native:* ids,
  // surfaced from _kiro.dev/subagent/list_update) are nested UNDER a managed
  // agent, not launched by this session's spawn_run, so counting them inflated
  // the histogram past what was actually spawned (e.g. 4 spawned → 9 tracked).
  // They remain fully visible in the Subagents activity sidebar.
  const all = useMemo(() => Object.values(subagents).filter(a => !a.id.startsWith('native:')), [subagents])
  // Exception-first ordering: retrying/stalled agents need eyes; the healthy
  // majority collapses behind the summary row at scale.
  const activeList = useMemo(() => {
    const act = all.filter(a => a.status === 'running' || a.status === 'tool' || a.status === 'pending')
    const rank = (a: SubagentActivity) => (a.retrying ? 0 : a.stalled ? 1 : a.status === 'pending' ? 2 : 3)
    return act.sort((x, y) => rank(x) - rank(y))
  }, [all])
  const running = activeList.length
  // Histogram counts across the WHOLE wave (terminal agents included) so a
  // failure mid-wave is visible in the header instead of silently dropping
  // out of the running-only list.
  const counts = useMemo(() => ({
    done: all.filter(a => a.status === 'done').length,
    failed: all.filter(a => a.status === 'error').length,
    stopped: all.filter(a => a.status === 'stopped').length,
    stalled: activeList.filter(a => a.stalled).length,
  }), [all, activeList])
  // `all` already excludes native:* ids, so error entries here are managed.
  const failedIds = useMemo(() => all.filter(a => a.status === 'error').map(a => a.id), [all])
  const activeListRef = useRef(activeList)
  activeListRef.current = activeList
  // Mount when anything is in flight — running OR queued. Including queued is
  // what makes the chip (1) appear the instant a wave is accepted, before the
  // first agent's subagent_spawn arrives, and (2) stay mounted across the
  // staggered ramp instead of flickering out whenever running momentarily hits
  // zero between staggered starts.
  const hasActive = running > 0 || queued > 0
  const visibleList = activeList.slice(0, CHIP_MAX_ROWS)
  const hiddenCount = activeList.length - visibleList.length
  // Only running/tool agents are cancellable via spawnDelete; pending agents
  // (awaiting approval) are resolved through the approval reject path instead.
  const stoppableCount = useMemo(() => activeList.filter(a => a.status === 'running' || a.status === 'tool').length, [activeList])
  // Cancel a running subagent. A failed spawnDelete is swallowed with only a
  // debug breadcrumb. The 30s reconcile loop below is the safety net that
  // drops any agent the backend actually stopped.
  const stopAgent = useCallback((id: string) => {
    api.spawnDelete(id).catch(() => console.warn(`spawnDelete failed for subagent ${id}; reconcile loop will resync`))
  }, [])
  const stopAll = useCallback(() => {
    activeListRef.current.forEach(a => { if (a.status === 'running' || a.status === 'tool') stopAgent(a.id) })
  }, [stopAgent])
  const [retrying, setRetrying] = useState(false)
  // Collapse the agent list to the one-line header. Default expanded; the
  // choice is remembered across sessions via localStorage so a user who
  // prefers the quiet header keeps it. Counts + Stop all stay in the header
  // either way, so a wave can always be stopped without expanding.
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(COLLAPSE_KEY) === '1' } catch { return false }
  })
  const toggleCollapsed = useCallback(() => {
    setCollapsed(c => {
      const next = !c
      try { localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0') } catch { /* private mode / quota — keep in-memory only */ }
      return next
    })
  }, [])
  const retryFailed = useCallback(() => {
    setRetrying(true)
    Promise.allSettled(failedIds.map(id => api.spawnRetry(id))).finally(() => setRetrying(false))
  }, [failedIds])
  const openAgent = useCallback((id: string) => {
    dispatch(selectSubagent(id))
    dispatch(openActivityToTab('subagents'))
  }, [dispatch])
  const [, setTick] = useState(0)
  // 1Hz tick to update elapsed timers + 30s reconciliation to clear phantom agents
  useEffect(() => {
    if (!hasActive || !slot) return
    let cancelled = false
    const t = setInterval(() => setTick(n => 1 - n), 1000)
    const reconcile = setInterval(() => {
      api.spawnList().then((d: SpawnListResponse) => {
        if (cancelled) return
        const backendIds = new Set((d.agents || []).filter((a) => !a.done && a.parent === `dashboard:${slot}`).map((a) => a.id))
        activeListRef.current.forEach(a => {
          if (!backendIds.has(a.id)) dispatch(sseSubagentDone({ slot, id: a.id, elapsed: Math.round((Date.now() - a.startedAt) / 1000), error: 'reconciliation: agent no longer tracked by backend' }))
        })
      }).catch(() => {})
    }, 30_000)
    return () => { cancelled = true; clearInterval(t); clearInterval(reconcile) }
  }, [hasActive, slot, dispatch])
  if (!hasActive) return null
  return (
    // `relative z-[46]` lifts the wave chip above every theme-experience
    // overlay: those are clamped to OVERLAY_Z_MAX=45 in ThemeExperienceLayer,
    // so 46 is the minimal value that no theme (built-in or custom, present or
    // future) can paint over — while staying below the mute button (z=50) and
    // consent modal (z=120), and under modal backdrops (z-[46], later in DOM).
    // Without this the chip sits at auto z-index and a fullscreen overlay (e.g.
    // an activate-time transition wipe) covers it for the overlay's lifetime.
    <div className="px-5 mx-auto w-full relative z-[46]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="mb-1 rounded-md bg-accent/10 border border-accent/20 animate-slide-up overflow-hidden">
        {/* Chrome type, so no `font-mono`: the wave chip is prose and labels,
            and Tailwind's `font-mono` pins `var(--mono)` — a token the Font
            Family setting never writes, so a hardcoded one here overrode the
            user's choice and put JetBrains Mono (no CJK coverage) under a
            translated UI. Mono is re-applied below on the parts that earn it:
            the tree glyphs, the elapsed/tool counter and the tool command. */}
        <div className="flex items-center gap-2 px-3 py-1.5 text-[13px]">
          <button
            type="button"
            onClick={toggleCollapsed}
            className="shrink-0 flex items-center text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm"
            aria-expanded={!collapsed}
            aria-label={collapsed ? i18nT('pages.chat.subagentProgressBar.expand_agent_list') : i18nT('pages.chat.subagentProgressBar.collapse_agent_list')}
            title={collapsed ? i18nT('pages.chat.subagentProgressBar.expand_agent_list') : i18nT('pages.chat.subagentProgressBar.collapse_agent_list')}
          >
            <ChevronRight size={14} className="transition-transform" style={{ transform: collapsed ? 'none' : 'rotate(90deg)' }} />
          </button>
          <Bot size={14} className="text-accent shrink-0" />
          {/* Histogram header: whole-wave counts so mid-wave failures stay visible */}
          <span className="text-text-strong font-medium flex items-center gap-2 min-w-0" data-testid="subagent-histogram">
            <span className="inline-flex items-center gap-1" data-testid="subagent-running-count"><Loader2 size={12} className="animate-spin text-accent" /> {running}</span>
            {queued > 0 && <span className="inline-flex items-center gap-1 text-muted" data-testid="subagent-queued-count" title={i18nT('pages.chat.subagentProgressBar.waiting_to_start_queued_behind_the_concurrency_l')}><Clock size={12} /> {queued}</span>}
            {counts.done > 0 && <span className="inline-flex items-center gap-1 text-ok"><CheckCircle size={12} /> {counts.done}</span>}
            {counts.failed > 0 && <span className="inline-flex items-center gap-1 text-danger"><AlertCircle size={12} /> {counts.failed}</span>}
            {counts.stopped > 0 && <span className="inline-flex items-center gap-1 text-muted"><Square size={12} /> {counts.stopped}</span>}
            {counts.stalled > 0 && <span className="inline-flex items-center gap-1 text-warn" title={i18nT('pages.chat.subagentProgressBar.no_activity_possibly_stalled')}><AlertTriangle size={12} /> {counts.stalled}</span>}
          </span>
          <span className="ml-auto shrink-0 flex items-center gap-1.5">
            {failedIds.length > 0 && (
              <button
                className="shrink-0 flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border border-accent/40 text-accent/80 hover:bg-accent/10 hover:text-accent cursor-pointer transition-all bg-transparent disabled:opacity-50"
                onClick={retryFailed}
                disabled={retrying}
                aria-label={`Retry ${failedIds.length} failed subagent${failedIds.length > 1 ? 's' : ''}`}
              >
                <RotateCcw size={11} className={retrying ? 'animate-spin' : ''} /> {i18nT('pages.chat.subagentProgressBar.retry_failed_count', { count: failedIds.length })}
              </button>
            )}
            {stoppableCount > 0 && (
              <button
                className="shrink-0 flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border border-danger/40 text-danger/70 hover:bg-danger-subtle hover:text-danger cursor-pointer transition-all bg-transparent"
                onClick={stopAll}
                aria-label={stoppableCount > 1 ? i18nT('pages.chat.subagentProgressBar.stop_all_running_subagents') : i18nT('pages.chat.subagentProgressBar.stop_running_subagent')}
              >
                <X size={11} /> {i18nT('pages.chat.subagentProgressBar.stop')}{stoppableCount > 1 ? ' all' : ''}
              </button>
            )}
          </span>
        </div>
        <div className={`px-3 pb-2 space-y-0.5${collapsed ? ' hidden' : ''}`}>
          {visibleList.map((a, i) => {
            const isLast = i === visibleList.length - 1 && hiddenCount === 0
            const taskPreview = sanitizeLlmOutput((a.task || '').slice(0, 80)) + ((a.task || '').length > 80 ? '…' : '')
            const agentLabel = taskPreview || sanitizeLlmOutput(a.agent || 'agent')
            const elapsed = Math.round((Date.now() - a.startedAt) / 1000)
            const stoppable = a.status === 'running' || a.status === 'tool'
            return (
              <div key={a.id} data-testid="subagent-row" className="flex items-start gap-1">
                <button
                  type="button"
                  className="min-w-0 flex-1 flex items-start gap-1.5 rounded-sm text-left text-[12px] text-muted hover:bg-accent/5 transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
                  onClick={() => openAgent(a.id)}
                  aria-label={i18nT('pages.chat.subagentProgressBar.open_in_subagents_sidebar', { label: agentLabel })}
                >
                  {/* Box-drawing glyphs stay mono so `├─` and `└─` keep an
                      identical advance width and the rows line up. */}
                  <span aria-hidden="true" className="shrink-0 font-mono text-border select-none">{isLast ? '└─' : '├─'}</span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="min-w-0 flex-1 truncate text-text">{agentLabel}</span>
                      <span className="shrink-0 font-mono tabular-nums text-muted/50">{elapsed}{i18nT('pages.chat.subagentProgressBar.s')}{typeof a.toolCount === 'number' && a.toolCount > 0 ? ` · ${a.toolCount} tool${a.toolCount > 1 ? 's' : ''}` : ''}</span>
                    </span>
                    {a.retrying ? (
                      <span className="text-info flex items-center gap-1">
                        <Loader2 size={11} className="shrink-0 animate-spin" />
                        <span className="truncate">{i18nT('pages.chat.subagentProgressBar.backend_hiccup_retrying')}</span>
                      </span>
                    ) : a.stalled ? (
                      <span className="text-warn flex items-center gap-1">
                        <AlertTriangle size={11} className="shrink-0" />
                        {/* The tool name carries the same mono as the non-stalled
                            `→ lastTool` line below — the two render the SAME value
                            and diverged once the parent stopped supplying it. */}
                        <span className="truncate">{i18nT('pages.chat.subagentProgressBar.stalled')}{a.lastTool ? <span className="font-mono">{` at ${sanitizeLlmOutput(a.lastTool)}`}</span> : ''} {i18nT('pages.chat.subagentProgressBar.no_activity')}</span>
                      </span>
                    ) : (a.lastTool && <span className="block font-mono text-accent/60 truncate">→ {sanitizeLlmOutput(a.lastTool)}</span>)}
                  </span>
                </button>
                {stoppable && (
                  <button
                    className="shrink-0 flex items-center text-[11px] px-1 py-0.5 rounded border border-danger/40 text-danger/70 hover:bg-danger-subtle hover:text-danger cursor-pointer transition-all bg-transparent"
                    onClick={() => stopAgent(a.id)}
                    aria-label={i18nT('pages.chat.subagentProgressBar.stop_subagent', { name: sanitizeLlmOutput(a.agent || a.id) })}
                    title={i18nT('pages.chat.subagentProgressBar.stop_this_subagent')}
                  >
                    <X size={11} />
                  </button>
                )}
              </div>
            )
          })}
          {hiddenCount > 0 && (
            <button
              type="button"
              data-testid="subagent-overflow-row"
              className="w-full flex items-center gap-1.5 rounded-sm text-left text-[12px] text-muted/60 hover:bg-accent/5 transition-colors cursor-pointer bg-transparent border-none"
              onClick={() => dispatch(openActivityToTab('subagents'))}
              aria-label={`Show ${hiddenCount} more running subagents in the sidebar`}
            >
              <span aria-hidden="true" className="shrink-0 font-mono text-border select-none">└─</span>
              <span>+ {hiddenCount} {i18nT('pages.chat.subagentProgressBar.more_running_normally')}</span>
            </button>
          )}
        </div>
      </div>
    </div>
  )
})

export default SubagentProgressBar
