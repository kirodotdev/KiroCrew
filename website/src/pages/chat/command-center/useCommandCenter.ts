import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { useAppSelector } from '../../../store'
import type { Artifact, SubagentActivity } from '../../../types'
import { buildCommandCenter, effectiveApprovalMode, scopedSlots, slotKey, type PendingQuestion, type WorkItem } from './model'

export const TASK_DASHBOARD_TAG = 'task-dashboard'
const POLL_MS = 10_000
const EMPTY_AGENTS: Record<string, SubagentActivity> = {}

/** Shared query keys let the dock and panel observe one poll, not one per worker. */
export function useCommandCenter(root: string | null, enabled = true, scope: 'task' | 'fleet' = 'task') {
  const slots = useAppSelector(s => s.dashboard.slots)
  const approvalMode = useAppSelector(s => s.dashboard.approvalMode)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const liveAgents = useAppSelector(s => s.chat.subagents)
  const background = useAppSelector(s => s.chat.slotActivity)
  const liveWorkflows = useAppSelector(s => s.chat.workflowRuns)
  const connected = useAppSelector(s => s.dashboard.connected)
  const fleet = scope === 'fleet'
  const scoped = useMemo(() => fleet ? slots : root ? scopedSlots(slots, root) : [], [slots, root, fleet])
  const canRead = enabled && (fleet || !!root && scoped.length > 0)
  const polling = { enabled: canRead, staleTime: 3_000, refetchInterval: POLL_MS }
  const questions = useQuery({ queryKey: ['command-center', 'questions'], queryFn: api.pendingQuestions, ...polling })
  // Only the mounted owner's actively drafted STATELESS cards survive retirement.
  // This is presentation continuity, never a cache of live approval/ask authority.
  const draftScope = JSON.stringify([scope, root])
  const [drafts, setDrafts] = useState<{ scope: string; cards: Record<string, PendingQuestion> }>({ scope: draftScope, cards: {} })
  if (drafts.scope !== draftScope) setDrafts({ scope: draftScope, cards: {} })
  const onQuestionDraftChange = (question: PendingQuestion, active: boolean) => {
    if (question.ask_id || !question.card_id) return
    const id = JSON.stringify([slotKey(question.slot), question.card_id])
    setDrafts(previous => {
      // A departing card's cleanup must not clear a new scope's draft.
      if (previous.scope !== draftScope) return previous
      if (active) return previous.cards[id] === question ? previous : { ...previous, cards: { ...previous.cards, [id]: question } }
      if (!previous.cards[id]) return previous
      const cards = { ...previous.cards }
      delete cards[id]
      return { ...previous, cards }
    })
  }
  const visibleQuestions = useMemo(() => {
    const live = questions.data || []
    const ids = new Set(live.map(q => JSON.stringify([slotKey(q.slot), q.card_id])))
    return [...live, ...Object.values(drafts.scope === draftScope ? drafts.cards : {}).filter(q => !ids.has(JSON.stringify([slotKey(q.slot), q.card_id])))]
  }, [questions.data, drafts, draftScope])
  const approvals = useQuery({ queryKey: ['command-center', 'approvals'], queryFn: api.approvals, ...polling })
  const workflows = useQuery({ queryKey: ['command-center', 'workflows'], queryFn: api.workflowRuns, ...polling })
  const work = useQuery({
    queryKey: ['command-center', root, 'work'],
    queryFn: () => api.sessionWorkProjection(root!) as Promise<{ value?: { items: WorkItem[]; omitted?: number } }>,
    ...polling, enabled: canRead && !fleet,
  })
  const artifacts = useQuery({
    queryKey: ['command-center', 'artifacts'],
    queryFn: () => api.artifacts({ tag: TASK_DASHBOARD_TAG }) as Promise<{ artifacts?: Artifact[] }>,
    ...polling,
  })
  const model = useMemo(() => {
    const subagents = Object.fromEntries(scoped.map(s => [s.key,
      s.key === activeSlot ? liveAgents : background?.[s.key]?.subagents || EMPTY_AGENTS,
    ]))
    // REST restores completed runs after a reload; live events win until the next
    // authoritative poll. Neither an unavailable endpoint nor an idle slot is success.
    const runs = new Map((workflows.data?.runs || []).map(r => [r.run_id, r]))
    for (const r of Object.values(liveWorkflows || {})) {
      runs.set(r.run_id, { ...runs.get(r.run_id), run_id: r.run_id, name: r.name, status: r.status,
        session_key: r.sessionKey || runs.get(r.run_id)?.session_key || '', error: r.error })
    }
    return buildCommandCenter({ root: fleet ? null : root, slots: scoped, subagents, approvalMode, workflows: [...runs.values()],
      questions: visibleQuestions, approvals: approvals.data || [], work: fleet ? undefined : work.data?.value })
  }, [root, scoped, activeSlot, liveAgents, background, liveWorkflows, workflows.data, visibleQuestions, approvals.data, work.data, approvalMode, fleet])
  const scopedKeys = new Set(scoped.map(s => s.key))
  const dashboards = (artifacts.data?.artifacts || []).filter(a =>
    (a.kind === 'html' || a.kind === 'widget') && a.tags.includes(TASK_DASHBOARD_TAG)
    && !!a.session_key && scopedKeys.has(slotKey(a.session_key)),
  )
  const sources = fleet ? [questions, approvals, workflows, artifacts] : [questions, approvals, workflows, work, artifacts]
  return {
    ...model, dashboards, connected, onQuestionDraftChange,
    approvalMode: effectiveApprovalMode(approvalMode, slots.find(s => s.key === root)),
    loading: canRead && sources.some(q => q.isPending),
    stale: canRead && (!connected || sources.some(q => q.isError)),
    // Real clock from completed reads. A websocket connection alone doesn't
    // establish that a server-side question/approval inventory is up to date.
    updatedAt: Math.min(...sources.map(q => q.dataUpdatedAt)),
    approvalCount: model.attention.filter(a => a.kind === 'approval').length,
    relevant: scoped.length > 1 || model.nodes.some(n => n.kind !== 'session') || model.workItems.length > 0 || dashboards.length > 0 || model.attention.some(a => a.kind === 'approval'),
  }
}

export type CommandCenterData = ReturnType<typeof useCommandCenter>
