import { useState, useRef, useEffect, useMemo, useCallback, memo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bot, X, AlertTriangle, Loader2, CheckCircle, AlertCircle, Square, RotateCcw, Clock, ChevronRight, Check } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { openActivityToTab, selectSubagent, sseSubagentDone, sseSubagentSpawn, sseSubagentUpdateMeta, clearTerminalSubagents } from '../../store/chatSlice'
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
  error?: string
  stopped?: boolean
  outcome?: 'completed' | 'failed' | 'stopped'
  parent?: string
  depth?: number
  task?: string
  agent?: string
  started?: number
  elapsed?: number
}
interface SpawnListResponse {
  agents?: SpawnListAgent[]
}

/** Reconcile grace period — agents younger than this are not phantom-pruned. */
const RECONCILE_GRACE_MS = 15_000
/** Base polling interval for the adaptive reconcile loop. */
const RECONCILE_BASE_MS = 5_000
/** If no tree change in this window, every Nth tick fires (backoff). */
const RECONCILE_IDLE_MS = 30_000
/** Only fire every 3rd tick when idle (effective ~15s). */
const RECONCILE_IDLE_DIVISOR = 3

/**
 * Truncation-vs-tree decision: when the flat visible list exceeds CHIP_MAX_ROWS,
 * we apply tree-aware truncation that never silently orphans visible children.
 * Strategy: walk the tree in pre-order; cut at CHIP_MAX_ROWS but if the cut
 * would separate a parent from its children that are already rendered, extend
 * to include those children. If THAT would exceed CHIP_MAX_ROWS + 2, trim
 * from the deepest leaves instead. The overflow row always counts the hidden
 * remainder.
 */

/** Active subagent summary above the chat input. */
const SubagentProgressBar = memo(function SubagentProgressBar({ slot }: { slot: string | null }) {
  const dispatch = useAppDispatch()
  const subagents = useAppSelector(s => slot === s.chat.activeSlot ? s.chat.subagents : s.chat.slotActivity[slot ?? '']?.subagents ?? EMPTY_SUBAGENTS)
  const queued = useAppSelector(s => s.chat.subagentQueued?.[slot ?? ''] ?? 0)

  // Include ALL agents (including done) in the display list for tree spine.
  // Filter out native:* from the chip count and the rendered rows below.
  const all = useMemo(() => Object.values(subagents).filter(a => !a.id.startsWith('native:')), [subagents])

  const activeList = useMemo(() => {
    return all.filter(a => a.status === 'running' || a.status === 'tool' || a.status === 'pending')
  }, [all])
  const running = activeList.length

  const counts = useMemo(() => ({
    done: all.filter(a => a.status === 'done').length,
    failed: all.filter(a => a.status === 'error').length,
    stopped: all.filter(a => a.status === 'stopped').length,
    stalled: activeList.filter(a => a.stalled).length,
  }), [all, activeList])

  const failedIds = useMemo(() => all.filter(a => a.status === 'error').map(a => a.id), [all])

  const hasActive = running > 0 || queued > 0

  // --- Tree ordering: pre-order walk producing a flat list ---
  // orderedList includes done agents so intermediate managers stay visible
  // as the greyscale spine (their children still need the chain).
  const { orderedList, lastIds, maxDepth } = useMemo(() => {
    const present = new Set(all.map(a => a.id))
    const kids: Record<string, SubagentActivity[]> = {}
    all.forEach(a => { const p = a.parentKey || ''; (kids[p] ||= []).push(a) })
    // Sort each sibling group by startedAt (chronological)
    Object.values(kids).forEach(arr => arr.sort((x, y) => (x.startedAt || 0) - (y.startedAt || 0)))
    // Identify last-child per group for connector selection
    const lastIds = new Set<string>()
    Object.values(kids).forEach(arr => { if (arr.length) lastIds.add(arr[arr.length - 1].id) })
    const out: SubagentActivity[] = []
    const seen = new Set<string>()
    const walk = (parentKey: string) => {
      (kids[parentKey] || []).forEach(a => {
        if (seen.has(a.id)) return
        seen.add(a.id); out.push(a); walk(`subagent:${a.id}`)
      })
    }
    // Roots: nodes whose parent is NOT another present node
    all.forEach(a => {
      const pk = a.parentKey || ''
      const parentPresent = pk.startsWith('subagent:') && present.has(pk.slice(9))
      if (!parentPresent && !seen.has(a.id)) { seen.add(a.id); out.push(a); walk(`subagent:${a.id}`) }
    })
    // Safety: any unreached nodes (circular ref)
    all.forEach(a => { if (!seen.has(a.id)) { seen.add(a.id); out.push(a) } })
    let md = 1
    for (const a of out) { if (a.depth && a.depth > md) md = a.depth }
    return { orderedList: out, lastIds, maxDepth: md }
  }, [all])

  // For the chip, only show active agents, but in tree order
  const activeInOrder = useMemo(() => {
    const activeIds = new Set(activeList.map(a => a.id))
    // Show active agents plus their ancestors if done (greyscale spine)
    // to maintain tree visual coherence. But cap at CHIP_MAX_ROWS.
    const needed = new Set<string>()
    const parentOf: Record<string, string> = {}
    all.forEach(a => { if (a.parentKey?.startsWith('subagent:')) parentOf[a.id] = a.parentKey.slice(9) })
    // Walk from each active agent up to root, collecting needed spine nodes
    for (const id of activeIds) {
      let cur: string | undefined = id
      let guard = 0
      while (cur && guard++ < 32) {
        needed.add(cur)
        cur = parentOf[cur]
      }
    }
    return orderedList.filter(a => needed.has(a.id))
  }, [orderedList, activeList, all])

  // Tree-aware truncation: a plain prefix slice is already orphan-safe.
  // `orderedList` is a DFS pre-order walk, so an ancestor always precedes its
  // descendants; `needed` is ancestor-closed (the walk above adds every parent
  // up to the root), and filtering preserves relative order. Therefore any
  // PREFIX of activeInOrder is itself ancestor-closed — a visible child can
  // never lose its visible parent, so no extension pass is required.
  const visibleList = useMemo(() => {
    if (activeInOrder.length <= CHIP_MAX_ROWS) return activeInOrder
    return activeInOrder.slice(0, CHIP_MAX_ROWS)
  }, [activeInOrder])
  const hiddenCount = activeInOrder.length - visibleList.length

  const stoppableCount = useMemo(() => activeList.filter(a => a.status === 'running' || a.status === 'tool').length, [activeList])

  const activeListRef = useRef(activeList)
  activeListRef.current = activeList
  const subagentsRef = useRef(subagents)
  subagentsRef.current = subagents

  const stopAgent = useCallback((id: string) => {
    api.spawnDelete(id).catch(() => console.warn(`spawnDelete failed for subagent ${id}; reconcile loop will resync`))
  }, [])
  const stopAll = useCallback(() => {
    activeListRef.current.forEach(a => { if (a.status === 'running' || a.status === 'tool') stopAgent(a.id) })
  }, [stopAgent])
  const [retrying, setRetrying] = useState(false)
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(COLLAPSE_KEY) === '1' } catch { return false }
  })
  const toggleCollapsed = useCallback(() => {
    setCollapsed(c => {
      const next = !c
      try { localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0') } catch { /* private mode */ }
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

  // --- Reconcile / backfill with generation counter and adaptive polling ---
  // Timestamp of this pane's last run-boundary eviction. Any response whose
  // request STARTED at or before this instant is stale and must not be applied.
  const evictedAtRef = useRef(0)
  // Persistent parent chain: accumulates parent edges across polls so a
  // backend-evicted parent doesn't orphan its live descendants during the walk.
  const parentChainRef = useRef<Record<string, string>>({})
  const lastChangeRef = useRef(Date.now())
  const [, setTick] = useState(0)

  // Applies a /api/spawn snapshot to the store. Pure apply — the fetch and its
  // scheduling belong to React Query (below), so every pane shares one request.
  const applyReconcile = useCallback((d: SpawnListResponse) => {
    if (!slot) return
    {
      const root = `dashboard:${slot}`
      const allBackend = d.agents || []
      // Build parent index from ALL agents including done
      const parentOf: Record<string, string> = {}
      allBackend.forEach((a) => { if (a.parent) parentOf[a.id] = a.parent })
      // Merge into persistent chain so evicted parents remain walkable
      Object.assign(parentChainRef.current, parentOf)
      // Compute rooted-here subtree using persistent chain as fallback
      const rootsHere = (a: SpawnListAgent): boolean => {
        let p: string | undefined = a.parent
        let guard = 0
        while (p && guard++ < 32) {
          if (p === root) return true
          const childId = p.startsWith('subagent:') ? p.slice(9) : undefined
          if (!childId) break
          p = parentOf[childId] || parentChainRef.current[childId]
        }
        return false
      }
      const rootedAll = allBackend.filter(rootsHere)
      const rootedIds = new Set(rootedAll.map(a => a.id))
      let changed = false
      const known = subagentsRef.current

      // Backfill
      rootedAll.forEach((a) => {
        const cur = known[a.id]
        if (!cur) {
          // Reject terminal records whose agent started before the eviction
          // watermark — a later shared poll must not resurrect agents that
          // this pane already cleared at run-boundary.
          const agentStartMs = a.started ? Math.round(a.started * 1000) : 0
          if (a.done && agentStartMs > 0 && agentStartMs <= evictedAtRef.current) return
          dispatch(sseSubagentSpawn({
            slot, id: a.id, task: a.task || '', agent: a.agent || '',
            parent: a.parent, depth: a.depth, done: !!a.done,
            startedAt: a.started ? Math.round(a.started * 1000) : undefined,
            elapsed: a.elapsed,
          }))
          // Cold backfill: if the agent is already done, dispatch terminal state
          // so the reducer records outcome/error/stopped correctly (sseSubagentSpawn
          // only sets `done` without failure semantics).
          if (a.done) {
            dispatch(sseSubagentDone({
              slot, id: a.id,
              elapsed: a.elapsed ?? 0,
              ...(a.error ? { error: a.error } : {}),
              ...(a.stopped ? { stopped: a.stopped } : {}),
              ...(a.outcome ? { outcome: a.outcome } : {}),
            }))
          }
          changed = true
        } else {
          // Correct stale metadata
          if ((a.parent && cur.parentKey !== a.parent) || (a.depth != null && cur.depth !== a.depth)) {
            dispatch(sseSubagentUpdateMeta({ slot, id: a.id, parent: a.parent, depth: a.depth }))
            changed = true
          }
          // Backend says done but local still running
          if (a.done && cur.status !== 'done' && cur.status !== 'error' && cur.status !== 'stopped') {
            dispatch(sseSubagentDone({ slot, id: a.id, elapsed: a.elapsed ?? Math.round((Date.now() - cur.startedAt) / 1000), ...(a.error ? { error: a.error } : {}), ...(a.stopped ? { stopped: a.stopped } : {}), ...(a.outcome ? { outcome: a.outcome } : {}) }))
            changed = true
          }
        }
      })

      // Prune phantoms (only agents older than grace period)
      Object.values(known).forEach((a) => {
        if (a.id.startsWith('native:')) return
        if (Date.now() - a.startedAt < RECONCILE_GRACE_MS) return
        if (!rootedIds.has(a.id) && (a.status === 'running' || a.status === 'tool' || a.status === 'pending')) {
          dispatch(sseSubagentDone({ slot, id: a.id, elapsed: Math.round((Date.now() - a.startedAt) / 1000), error: 'reconciliation: agent no longer tracked by backend' }))
          changed = true
        }
      })

      if (changed) lastChangeRef.current = Date.now()
    }
  }, [slot, dispatch])

  // ONE shared poll for every pane. The query key is deliberately slot-free:
  // /api/spawn returns the whole fleet and each pane filters to its own root, so
  // keying by slot would make N split panes issue N identical requests. React
  // Query owns the fetch, dedupes concurrent observers, and drives the interval.
  //
  // Adaptive cadence: base while the tree is changing, backing off when idle.
  // With a shared key each observer keeps its own interval and React Query
  // refetches on the shortest, so an active pane is not slowed by an idle one.
  // `enabled` covers the cold-mount discovery fetch — a fresh page finds the
  // backend subtree without waiting for the first interval tick.
  //
  // `startedAt` is stamped INSIDE the payload because it is a property of the
  // request, not of an observer: the shared query is fetched once but read by
  // every pane, and each pane compares it against its OWN eviction watermark.
  const { data: spawnList, dataUpdatedAt } = useQuery({
    queryKey: ['subagent-spawn-list'],
    queryFn: async (): Promise<{ startedAt: number; agents: SpawnListResponse }> => {
      const startedAt = Date.now()
      return { startedAt, agents: await api.spawnList() }
    },
    enabled: !!slot,
    refetchOnWindowFocus: false,
    refetchInterval: () => {
      if (!hasActive) return false
      const idle = Date.now() - lastChangeRef.current > RECONCILE_IDLE_MS
      return idle ? RECONCILE_BASE_MS * RECONCILE_IDLE_DIVISOR : RECONCILE_BASE_MS
    },
  })

  // Apply each snapshot at most once, and never replay one that was already
  // in flight when this pane's run boundary cleared its terminal agents —
  // otherwise the response lands after the eviction and resurrects them.
  // Comparing the request's own start time against the eviction watermark is
  // what makes that safe; `dataUpdatedAt` alone cannot, since it records when
  // the response ARRIVED (always after the eviction) rather than when the
  // request began.
  const appliedAtRef = useRef(0)
  useEffect(() => {
    if (!spawnList || !dataUpdatedAt) return
    if (appliedAtRef.current === dataUpdatedAt) return
    appliedAtRef.current = dataUpdatedAt
    if (spawnList.startedAt <= evictedAtRef.current) return // predates the eviction
    applyReconcile(spawnList.agents)
  }, [spawnList, dataUpdatedAt, applyReconcile])

  // Run-boundary eviction: hasActive true->false clears terminal entries
  const prevHasActive = useRef(hasActive)
  useEffect(() => {
    if (prevHasActive.current && !hasActive && slot) {
      evictedAtRef.current = Date.now()
      dispatch(clearTerminalSubagents({ slot }))
    }
    prevHasActive.current = hasActive
  }, [hasActive, slot, dispatch])

  // 1s ticker so elapsed labels advance. The reconcile poll itself is React
  // Query's refetchInterval (above), not a hand-rolled setInterval.
  useEffect(() => {
    if (!slot || !hasActive) return
    const t = setInterval(() => setTick(n => 1 - n), 1000)
    return () => clearInterval(t)
  }, [slot, hasActive])

  if (!hasActive) return null

  return (
    <div className="px-5 mx-auto w-full relative z-[46]" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="mb-1 rounded-md bg-accent/10 border border-accent/20 animate-slide-up overflow-hidden">
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
          {/* Histogram header */}
          <span className="text-text-strong font-medium flex items-center gap-2 min-w-0" data-testid="subagent-histogram">
            <span className="inline-flex items-center gap-1" data-testid="subagent-running-count"><Loader2 size={12} className="animate-spin text-accent" /> {running}</span>
            {queued > 0 && <span className="inline-flex items-center gap-1 text-muted" data-testid="subagent-queued-count" title={i18nT('pages.chat.subagentProgressBar.waiting_to_start_queued_behind_the_concurrency_l')}><Clock size={12} /> {queued}</span>}
            {counts.done > 0 && <span className="inline-flex items-center gap-1 text-ok"><CheckCircle size={12} /> {counts.done}</span>}
            {counts.failed > 0 && <span className="inline-flex items-center gap-1 text-danger"><AlertCircle size={12} /> {counts.failed}</span>}
            {counts.stopped > 0 && <span className="inline-flex items-center gap-1 text-muted"><Square size={12} /> {counts.stopped}</span>}
            {counts.stalled > 0 && <span className="inline-flex items-center gap-1 text-warn" title={i18nT('pages.chat.subagentProgressBar.no_activity_possibly_stalled')}><AlertTriangle size={12} /> {counts.stalled}</span>}
            {maxDepth > 1 && <span className="inline-flex items-center gap-1 text-muted text-[11px]" data-testid="subagent-depth-indicator">{maxDepth} {i18nT('pages.chat.subagentProgressBar.levels')}</span>}
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
          {visibleList.map((a) => {
            const depth = a.depth || 1
            const isLast = lastIds.has(a.id)
            const isDone = a.status === 'done' || a.status === 'error' || a.status === 'stopped'
            const taskPreview = sanitizeLlmOutput((a.task || '').slice(0, 80)) + ((a.task || '').length > 80 ? '…' : '')
            const agentLabel = taskPreview || sanitizeLlmOutput(a.agent || 'agent')
            const elapsed = isDone ? Math.round(a.elapsed || 0) : Math.round((Date.now() - a.startedAt) / 1000)
            const stoppable = a.status === 'running' || a.status === 'tool'
            const connector = isLast ? '└─' : '├─'
            return (
              <div key={a.id} data-testid="subagent-row" className={`flex items-start gap-1${isDone ? ' opacity-45' : ''}`}>
                <button
                  type="button"
                  className="min-w-0 flex-1 flex items-start gap-1.5 rounded-sm text-left text-[12px] text-muted hover:bg-accent/5 transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
                  onClick={() => openAgent(a.id)}
                  aria-label={i18nT('pages.chat.subagentProgressBar.open_in_subagents_sidebar', { label: agentLabel })}
                >
                  {/* Depth indentation + tree connector */}
                  <span aria-hidden="true" className="shrink-0 font-mono text-border select-none" style={{ paddingLeft: (depth - 1) * 14 }}>{connector}</span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      {isDone && <Check size={10} className="shrink-0 text-ok" aria-label="completed" />}
                      <span className="min-w-0 flex-1 truncate text-text">{agentLabel}</span>
                      <span className="shrink-0 font-mono tabular-nums text-muted/50">{elapsed}{i18nT('pages.chat.subagentProgressBar.s')}{!isDone && typeof a.toolCount === 'number' && a.toolCount > 0 ? ` · ${a.toolCount} tool${a.toolCount > 1 ? 's' : ''}` : ''}</span>
                    </span>
                    {!isDone && (a.retrying ? (
                      <span className="text-info flex items-center gap-1">
                        <Loader2 size={11} className="shrink-0 animate-spin" />
                        <span className="truncate">{i18nT('pages.chat.subagentProgressBar.backend_hiccup_retrying')}</span>
                      </span>
                    ) : a.stalled ? (
                      <span className="text-warn flex items-center gap-1">
                        <AlertTriangle size={11} className="shrink-0" />
                        <span className="truncate">{i18nT('pages.chat.subagentProgressBar.stalled')}{a.lastTool ? <span className="font-mono">{` at ${sanitizeLlmOutput(a.lastTool)}`}</span> : ''} {i18nT('pages.chat.subagentProgressBar.no_activity')}</span>
                      </span>
                    ) : (a.lastTool && <span className="block font-mono text-accent/60 truncate">→ {sanitizeLlmOutput(a.lastTool)}</span>))}
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
