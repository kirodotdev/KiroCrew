import { useState, useRef, useEffect, useMemo, useCallback, memo } from 'react'
import { Bot, X, AlertTriangle, Loader2 } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { openActivityToTab, sseSubagentDone } from '../../store/chatSlice'
import { api } from '../../api/client'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { SubagentActivity } from '../../types'

const EMPTY_SUBAGENTS: Record<string, SubagentActivity> = {}

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
  const activeList = useMemo(() => Object.values(subagents).filter(a => a.status === 'running' || a.status === 'tool' || a.status === 'pending'), [subagents])
  const running = activeList.length
  const anyStalled = useMemo(() => activeList.some(a => a.stalled), [activeList])
  const activeListRef = useRef(activeList)
  activeListRef.current = activeList
  const hasActive = running > 0
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
    <div className="px-5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="mb-1 rounded-md bg-accent/10 border border-accent/20 animate-slide-up overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-1.5 text-[13px] font-mono">
          <Bot size={14} className="text-accent shrink-0" />
          <span className="text-text-strong font-medium">{running} agent{running > 1 ? 's' : ''} running</span>
          {anyStalled && (
            <span className="ml-auto shrink-0 inline-flex items-center gap-1 text-warn" title="No activity — possibly stalled">
              <AlertTriangle size={12} /> stalled
            </span>
          )}
          {stoppableCount > 0 && (
            <button
              className={`${anyStalled ? '' : 'ml-auto '}shrink-0 flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded border border-danger/40 text-danger/70 hover:bg-danger-subtle hover:text-danger cursor-pointer transition-all bg-transparent`}
              onClick={stopAll}
              aria-label={stoppableCount > 1 ? 'Stop all running subagents' : 'Stop running subagent'}
            >
              <X size={11} /> Stop{stoppableCount > 1 ? ' all' : ''}
            </button>
          )}
        </div>
        <div className="px-3 pb-2 space-y-0.5">
          {activeList.map((a, i) => {
            const isLast = i === activeList.length - 1
            const taskPreview = sanitizeLlmOutput((a.task || '').slice(0, 80)) + ((a.task || '').length > 80 ? '…' : '')
            const agentLabel = taskPreview || sanitizeLlmOutput(a.agent || 'agent')
            const elapsed = Math.round((Date.now() - a.startedAt) / 1000)
            const stoppable = a.status === 'running' || a.status === 'tool'
            return (
              <div key={a.id} data-testid="subagent-row" className="flex items-start gap-1">
                <button
                  type="button"
                  className="min-w-0 flex-1 flex items-start gap-1.5 rounded-sm text-left text-[12px] text-muted font-mono hover:bg-accent/5 transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
                  onClick={() => dispatch(openActivityToTab('subagents'))}
                  aria-label={`Open ${agentLabel} in subagents sidebar`}
                >
                  <span aria-hidden="true" className="shrink-0 text-border select-none">{isLast ? '└─' : '├─'}</span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="min-w-0 flex-1 truncate text-text">{agentLabel}</span>
                      <span className="shrink-0 tabular-nums text-muted/50">{elapsed}s{typeof a.toolCount === 'number' && a.toolCount > 0 ? ` · ${a.toolCount} tool${a.toolCount > 1 ? 's' : ''}` : ''}</span>
                    </span>
                    {a.retrying ? (
                      <span className="text-info flex items-center gap-1">
                        <Loader2 size={11} className="shrink-0 animate-spin" />
                        <span className="truncate">backend hiccup — retrying…</span>
                      </span>
                    ) : a.stalled ? (
                      <span className="text-warn flex items-center gap-1">
                        <AlertTriangle size={11} className="shrink-0" />
                        <span className="truncate">stalled{a.lastTool ? ` at ${sanitizeLlmOutput(a.lastTool)}` : ''} — no activity</span>
                      </span>
                    ) : (a.lastTool && <span className="block text-accent/60 truncate">→ {sanitizeLlmOutput(a.lastTool)}</span>)}
                  </span>
                </button>
                {stoppable && (
                  <button
                    className="shrink-0 flex items-center text-[11px] px-1 py-0.5 rounded border border-danger/40 text-danger/70 hover:bg-danger-subtle hover:text-danger cursor-pointer transition-all bg-transparent"
                    onClick={() => stopAgent(a.id)}
                    aria-label={`Stop subagent ${sanitizeLlmOutput(a.agent || a.id)}`}
                    title="Stop this subagent"
                  >
                    <X size={11} />
                  </button>
                )}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
})

export default SubagentProgressBar
