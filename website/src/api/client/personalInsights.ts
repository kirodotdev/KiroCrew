/**
 * Personal Insights: the owner's private retrospective and its one-click
 * actions. Read routes return the latest run (or one by id) with its actions
 * and their live state; the two mutations apply a recommendation through the
 * typed host path (lesson store) and undo it. Both are owner-only on the
 * backend; an app token is refused with 403.
 */

import type { ClientTransport } from './transport'

export type InsightsActionState =
  | 'proposed'
  | 'applied_verified'
  | 'changed_unverified'
  | 'held'
  | 'undone'
  | 'deduped'
  | string

export type InsightsAction = {
  action_id: string
  action_key: string | null
  action_class: 'lesson_proposal' | 'prompt' | 'steering_patch' | 'existing_capability'
  behavior_predicate: string | null
  title: string
  cta: string
  executable: boolean
  why: string
  display_artifact: string
  expected_observation: string
  verification: string
  undo: string
  rank: number | null
  claim_ids: string[] | null
  state: InsightsActionState
  applied_at: number | null
  verified_at: number | null
  undone_at: number | null
  evidence_sessions: number
  evidence_total: number
  evidence_keys: string[]
}

export type InsightsPriorAction = {
  run_id: string
  action_id: string
  action_class: string
  title: string
  state: InsightsActionState
  applied_at: number | null
  verified_at: number | null
  baseline_sessions: number | null
}

export type InsightsRunSummary = {
  run_id: string
  created_at: number
  analyzed: number
  status: string
  artifact_slug: string | null
}

export type InsightsView = {
  run: InsightsRunSummary & { window_days: number; cataloged: number; served_model: string | null }
  actions: InsightsAction[]
  prior_actions: InsightsPriorAction[]
  report_before: string
  report_after: string
  runs: InsightsRunSummary[]
  follow_through_min_sessions: number
}

export type InsightsOverfit = {
  passed: boolean
  supporting_sessions: number
  independent_lineages: number
  distinct_days: number
  largest_lineage_share: number
  guidance_overlap: number
  duplicate_method: string
  overlapping_guidance: string[]
  reasons: string[]
}

export type InsightsApplyResult = {
  action_id: string
  state: InsightsActionState
  message: string
  overfit?: InsightsOverfit | null
  lesson_rule?: string | null
  verified?: boolean
  undo_available?: boolean
  superseded?: string[]
}

export function createPersonalInsightsEndpoints({ post, j, jfetch: fetch }: ClientTransport) {
  const personalInsights = {
    personalInsightsLatest: (): Promise<InsightsView> =>
      fetch('/api/personal-insights/latest').then(j),
    personalInsightsRun: (runId: string): Promise<InsightsView> =>
      fetch('/api/personal-insights/runs/' + encodeURIComponent(runId)).then(j),
    /** A 409 carries the held result (gate refused); the client surfaces it as a
     *  normal outcome rather than a transport failure. */
    personalInsightsDoIt: async (actionId: string, force = false): Promise<InsightsApplyResult> => {
      const r = await post('/api/personal-insights/actions/' + encodeURIComponent(actionId) + '/do-it', { force })
      if (r.status === 409) return (await r.json()) as InsightsApplyResult
      return j(r)
    },
    personalInsightsUndo: async (actionId: string): Promise<InsightsApplyResult> => {
      const r = await post('/api/personal-insights/actions/' + encodeURIComponent(actionId) + '/undo', {})
      if (r.status === 409) return (await r.json()) as InsightsApplyResult
      return j(r)
    },
  }
  return { personalInsights }
}
