import { useState, useRef, useEffect, useMemo, useCallback, memo } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Bot, X, AlertTriangle, Loader2, CheckCircle, AlertCircle, Square, RotateCcw, Clock, ChevronRight, Hand } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { openActivityToTab, selectSubagent, sseSubagentDone, isAwaitingSpawnApproval } from '../../store/chatSlice'
import { api } from '../../api/client'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { SubagentActivity } from '../../types'

import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
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

/**
 * Placeholder marker for the tool name inside a translated stall sentence.
 *
 * The stalled row used to be assembled in JSX from three fragments glued
 * together with a hardcoded English `" at "`, which stayed English in all 11
 * non-English locales (a Japanese user read
 * `停止している可能性があります at Running: sleep 600 — 117秒間アクティビティなし`)
 * and pinned the word order, so a locale whose grammar puts the tool elsewhere
 * could not express it. The sentence is now ONE interpolated catalog entry per
 * variant, so each locale orders it itself.
 *
 * The tool name still has to carry `font-mono` — it is the same value the
 * non-stalled `→ lastTool` row renders, and a plain interpolated string cannot
 * style a substring. So `{{tool}}` is interpolated with this sentinel and the
 * result split on it, which keeps BOTH properties: full translatability and the
 * monospace styling (and its regression test). U+0000 cannot occur in catalog
 * copy or in a sanitized tool name, so the split is unambiguous.
 */
const TOOL_SLOT = '\u0000'

/**
 * The four stall sentences, one per (tool present?) x (idle span present?).
 *
 * Full literal keys in an `as const` map rather than a template-built key, so
 * the i18n key gate can resolve every one statically and verify it exists --
 * a key that gate cannot resolve is exempt from every catalog check it runs.
 *
 * Four entries rather than one string with optional clauses, because a catalog
 * value cannot express "omit this clause": gluing the optional halves on
 * outside the translate call is exactly the defect being fixed here. The
 * no-span variants stay reachable -- a gateway too old to send `idle_secs`
 * still renders a complete, translated sentence.
 */
const STALL_KEYS = {
  tool_secs: 'pages.chat.subagentProgressBar.possibly_stalled_at_tool_for_secs',
  tool: 'pages.chat.subagentProgressBar.possibly_stalled_at_tool',
  secs: 'pages.chat.subagentProgressBar.possibly_stalled_for_secs',
  plain: 'pages.chat.subagentProgressBar.possibly_stalled_no_activity',
} as const

/**
 * Render a translated stall sentence, monospacing the tool name in place.
 *
 * Only the tool span is `truncate`. Previously the whole sentence truncated, so
 * a long tool name could clip off the idle figure — the very number that
 * justifies the warning — right off the end of the row.
 */
function StallText({ tool, idleSecs }: { tool: string; idleSecs?: number }) {
  const hasSecs = typeof idleSecs === 'number'
  // The key expression is inlined into the call, not bound to a local first, so
  // the i18n key gate can resolve every branch of it statically.
  const sentence = i18nT(
    tool
      ? (hasSecs ? STALL_KEYS.tool_secs : STALL_KEYS.tool)
      : (hasSecs ? STALL_KEYS.secs : STALL_KEYS.plain),
    { tool: TOOL_SLOT, secs: idleSecs },
  )
  const [before, after] = sentence.split(TOOL_SLOT)
  // No slot in the resolved string (the no-tool variants, or a catalog value
  // that dropped the placeholder) — render it whole rather than half.
  if (after === undefined) return <span className="min-w-0">{before}</span>
  return (
    <>
      {before && <span className="shrink-0 whitespace-pre">{before}</span>}
      <span className="min-w-0 truncate font-mono">{tool}</span>
      {after && <span className="shrink-0 whitespace-pre">{after}</span>}
    </>
  )
}

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
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
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
  // Runs PARKED on an unanswered spawn approval. They are registered and
  // active, but no process was ever launched — they are blocked on the user, so
  // they get their own tally instead of inflating the spinning running count
  // that told the user work was in progress (#7318).
  const awaiting = useMemo(() => activeList.filter(isAwaitingSpawnApproval).length, [activeList])
  const running = activeList.length - awaiting
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
  // Mount when anything is in flight — running OR queued OR parked on an
  // approval. Including queued is what makes the chip (1) appear the instant a
  // wave is accepted, before the first agent's subagent_spawn arrives, and (2)
  // stay mounted across the staggered ramp instead of flickering out whenever
  // running momentarily hits zero between staggered starts. `awaiting` is named
  // separately because it is no longer part of `running`: a wave whose only
  // member is parked on an approval has running === 0, and without this term
  // the chip — the one surface naming what is blocking it — would unmount.
  const hasActive = running > 0 || queued > 0 || awaiting > 0
  const visibleList = activeList.slice(0, CHIP_MAX_ROWS)
  const hiddenCount = activeList.length - visibleList.length
  // Per-row stop remains limited to agents with a live run id. The header also
  // includes accepted work waiting behind the stagger/concurrency queue. Pending
  // spawn approvals keep their explicit approve/reject path.
  const stoppableCount = useMemo(() => activeList.filter(a => a.status === 'running' || a.status === 'tool').length, [activeList])
  const stopTargetCount = stoppableCount + queued
  // Cancel a running subagent. A failed spawnDelete is swallowed with only a
  // debug breadcrumb. The 30s reconcile loop below is the safety net that
  // drops any agent the backend actually stopped.
  const stopAgent = useCallback((id: string) => {
    // eslint-disable-next-line no-console -- names which subagent refused to stop; the 30s reconcile loop hides the failure from the UI, so this is the only place a cancel that never landed is visible
    api.spawnDelete(id).catch(() => console.warn(`spawnDelete failed for subagent ${id}; reconcile loop will resync`))
  }, [])
  const stopAllMutation = useMutation({
    mutationFn: (targetSlot: string) => api.spawnStopAll(targetSlot),
    onError: (_error, targetSlot) => {
      // eslint-disable-next-line no-console -- the reconciliation loop will resync running cards, but queued work has no row id to retry individually
      console.warn(`spawnStopAll failed for slot ${targetSlot}; reconcile loop will resync`)
    },
  })
  const stopAll = useCallback(() => {
    if (!slot) return
    stopAllMutation.mutate(slot)
  }, [slot, stopAllMutation])
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
    // overlay. Overlays portal into the shell's decor slot, which is pinned at
    // OVERLAY_Z_MAX (lib/themeDecorLayer.ts — do not restate the number here);
    // the chip renders in the same shell stacking context, so 46 is a real
    // in-context comparison against that ceiling (the test pins 46 >
    // OVERLAY_Z_MAX). The mute button (z=50) and consent modal (z=120) live at
    // the document root and outrank the whole shell regardless; the chip also
    // stays under modal backdrops (z-[46], later in DOM).
    // Without this the chip sits at auto z-index and a fullscreen overlay (e.g.
    // an activate-time transition wipe) covers it for the overlay's lifetime.
    <div className="px-4 mx-auto w-full relative z-[46]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
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
            {awaiting > 0 && <span className="inline-flex items-center gap-1 text-warn" data-testid="subagent-awaiting-count" title={i18nT('pages.chat.subagentProgressBar.waiting_for_your_approval_to_start')}><Hand size={12} /> {awaiting}</span>}
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
              >
                {/* No aria-label: the accessible name IS the visible text below,
                    so WCAG 2.5.3 (Label in Name) holds by construction. */}
                <RotateCcw size={11} className={retrying ? 'animate-spin' : ''} /> {i18nT('pages.chat.subagentProgressBar.retry_failed_count', { count: failedIds.length })}
              </button>
            )}
            {stopTargetCount > 0 && (
              <button
                className="shrink-0 flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border border-danger/40 text-danger/70 hover:bg-danger-subtle hover:text-danger cursor-pointer transition-all bg-transparent"
                onClick={stopAll}
                aria-label={queued > 0 || stoppableCount > 1 ? i18nT('pages.chat.subagentProgressBar.stop_all') : i18nT('pages.chat.subagentProgressBar.stop_running_subagent')}
              >
                <X size={11} /> {queued > 0 || stoppableCount > 1 ? i18nT('pages.chat.subagentProgressBar.stop_all') : i18nT('pages.chat.subagentProgressBar.stop')}
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
            // The backend sends `idle_secs` once, on the stalled transition, so a
            // bare render would freeze at that value beside the live `elapsed`
            // above — the same two-numbers-disagree confusion this row exists to
            // remove. `stalled` means no activity by definition, so advancing it
            // locally from the receipt instant is sound. The 1Hz tick above
            // re-renders it.
            const idleShown = typeof a.idleSecs === 'number'
              ? a.idleSecs + (a.stalledAt ? Math.max(0, Math.round((Date.now() - a.stalledAt) / 1000)) : 0)
              : undefined
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
                      <span className="shrink-0 font-mono tabular-nums text-muted/50">{elapsed}{i18nT('pages.chat.subagentProgressBar.s')}{typeof a.toolCount === 'number' && a.toolCount > 0 ? ` · ${i18nT('pages.chat.subagentProgressBar.tool', { count: a.toolCount })}` : ''}</span>
                    </span>
                    {isAwaitingSpawnApproval(a) ? (
                      /* Checked BEFORE retrying/stalled: a parked run never
                         executed, so the watchdog's silence-based stall verdict
                         (and a retry attributed to a backend hiccup) would both
                         be describing an absence this row can already explain
                         exactly. Naming the approval is strictly more specific
                         than either, and it is the one state the user can act
                         on. */
                      <span className="text-warn flex items-center gap-1">
                        <Hand size={11} className="shrink-0" />
                        <span className="truncate">{i18nT('pages.chat.subagentProgressBar.waiting_for_your_approval_to_start')}</span>
                      </span>
                    ) : a.retrying ? (
                      <span className="text-info flex items-center gap-1">
                        <Loader2 size={11} className="shrink-0 animate-spin" />
                        <span className="truncate">{i18nT('pages.chat.subagentProgressBar.backend_hiccup_retrying')}</span>
                      </span>
                    ) : a.stalled ? (
                      <span className="text-warn flex items-center gap-1">
                        <AlertTriangle size={11} className="shrink-0" />
                        {/* Hedged to match the header tooltip: the watchdog sees
                            an ABSENCE of stream events, which a slow silent tool
                            also produces — it cannot prove a stall, so it must
                            not assert one. The idle span (not `elapsed`) is the
                            figure that justifies the warning, so it is shown
                            here; `elapsed` already sits on the row above and the
                            two are different numbers. The tool name carries the
                            same mono as the non-stalled `→ lastTool` line. */}
                        <span className="min-w-0 flex items-baseline">
                          <StallText
                            tool={a.lastTool ? sanitizeLlmOutput(a.lastTool) : ''}
                            idleSecs={idleShown}
                          />
                        </span>
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
