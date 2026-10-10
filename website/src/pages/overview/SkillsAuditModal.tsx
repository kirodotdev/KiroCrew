import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import { Btn } from '../../components/ui'
import { fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** Text-link styling for a shared `Btn`: twMerge drops Btn's own padding,
 *  border, background and press scale in favour of these classes. */
const AUDIT_LINK_CLASS =
  'inline-block min-w-0 max-w-full truncate rounded-none border-0 bg-transparent p-0 text-left text-[12px] text-accent underline underline-offset-2 hover:border-0 hover:bg-transparent hover:text-text-strong active:scale-100'

export interface SkillAuditMember {
  id: string
  kind: 'pending' | 'live'
  name: string
  slug?: string
}

export interface SkillAuditRelation {
  classification: 'duplicate' | 'subsumed' | 'overlapping'
  score: number
  members: [string, string]
}

export interface SkillAuditCluster {
  classification: SkillAuditRelation['classification']
  score: number
  members: SkillAuditMember[]
  relations: SkillAuditRelation[]
  update_targets: {
    pending_slug: string
    target: string
  }[]
  omitted_members?: number
  omitted_relations?: number
  omitted_update_targets?: number
}

const AUDIT_CLUSTER_RENDER_LIMIT = 12
const AUDIT_MEMBER_RENDER_LIMIT = 8

const AUDIT_CLASSIFICATION_KEY = {
  duplicate: 'pages.overview.skillsTab.audit_duplicate',
  subsumed: 'pages.overview.skillsTab.audit_subsumed',
  overlapping: 'pages.overview.skillsTab.audit_overlapping',
} as const

export type SkillAuditSelection = 'selected' | 'missing' | 'draft_open'

const AUDIT_SELECTION_PROBLEM_KEY = {
  missing: 'pages.overview.skillsTab.audit_member_missing',
  draft_open: 'pages.overview.skillsTab.audit_member_draft_open',
} as const

export default function SkillsAuditModal({
  open,
  onClose,
  onSelectMember,
  focus,
  askAgent = true,
}: {
  open: boolean
  onClose: () => void
  onSelectMember: (member: SkillAuditMember) => SkillAuditSelection
  focus?: { pendingSlug: string; target: string } | null
  /** False while the host holds an unsaved draft the hand-off would discard. */
  askAgent?: boolean
}) {
  const [selectionProblem, setSelectionProblem] = useState<
    Exclude<SkillAuditSelection, 'selected'> | null
  >(null)
  const queryClient = useQueryClient()
  const {
    data,
    isPending,
    error,
  } = useQuery<{
    clusters: SkillAuditCluster[]
    total_clusters?: number
    omitted_entries?: number
    omitted_relations?: number
  }>({
    queryKey: ['skills-audit'],
    queryFn: () => api.skillsAudit(),
    enabled: open,
    staleTime: 0,
  })

  const clusters = (data?.clusters ?? []).filter(cluster => {
    if (!focus) return true
    return cluster.members.some(
      member => member.kind === 'pending' && member.slug === focus.pendingSlug,
    ) && cluster.members.some(
      member => member.kind === 'live' && member.name === focus.target,
    )
  })
  const visibleClusters = clusters.slice(0, AUDIT_CLUSTER_RENDER_LIMIT)
  // The server returns a bounded list and reports the full count separately;
  // a focused view filters locally, so its own length is the total there.
  const totalClusters = focus
    ? clusters.length
    : Math.max(clusters.length, data?.total_clusters ?? 0)
  const omittedClusters = Math.max(0, totalClusters - visibleClusters.length)
  // Relations the audit dropped plus those the handler trimmed per cluster;
  // either kind of omission must be announced.
  const omittedRelations =
    (data?.omitted_relations ?? 0) +
    clusters.reduce((sum, cluster) => sum + (cluster.omitted_relations ?? 0), 0)

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={i18nT('pages.overview.skillsTab.audit_modal_title')}
      maxWidth={560}
    >
      <div className="space-y-2" data-testid="skills-audit-modal">
        {error && (
          <ErrorNotice
            variant="inline"
            askAgent={askAgent}
            message={(error as Error).message}
            testId="skills-audit-failure"
          />
        )}
        {!error && isPending && (
          <p className="text-[12px] text-muted" data-testid="skills-audit-loading">
            {i18nT('pages.overview.skillsTab.audit_loading')}
          </p>
        )}
        {!error && !isPending && clusters.length === 0 && (
          <p className="text-[12px] text-muted" data-testid="skills-audit-empty">
            {i18nT('pages.overview.skillsTab.audit_empty')}
          </p>
        )}
        {selectionProblem && (
          <ErrorNotice
            variant="inline"
            askAgent={askAgent}
            message={i18nT(AUDIT_SELECTION_PROBLEM_KEY[selectionProblem])}
            testId="skills-audit-selection-failure"
          />
        )}
        {visibleClusters.map(cluster => {
          const visibleMembers = cluster.members.slice(0, AUDIT_MEMBER_RENDER_LIMIT)
          const omittedMembers = Math.max(
            0,
            cluster.members.length - visibleMembers.length + (cluster.omitted_members ?? 0),
          )
          return (
            <div
              key={cluster.members.map(member => member.id).join('|')}
              className="rounded-md border border-border bg-card p-3"
            >
              <div className="text-[12px] font-semibold text-text-strong">
                {i18nT(AUDIT_CLASSIFICATION_KEY[cluster.classification])} ·{' '}
                <span data-testid="skills-audit-similarity">
                  {i18nT('pages.overview.skillsTab.audit_similarity', {
                    percent: fmtPercent(cluster.score),
                  })}
                </span>
              </div>
              {/* One member per row: each link is its own action, so no row
                  carries more than one control. */}
              <ul className="mt-1 space-y-0.5 text-[12px] text-muted">
                {visibleMembers.map(member => (
                  <li key={member.id} className="flex min-w-0 items-center gap-1.5">
                    {/* The shared Btn, restyled as a text link: twMerge lets these
                        classes override its padding, border and background. */}
                    <Btn
                      type="button"
                      className={AUDIT_LINK_CLASS}
                      translate="no"
                      title={member.name}
                      onClick={() => {
                        const outcome = onSelectMember(member)
                        setSelectionProblem(outcome === 'selected' ? null : outcome)
                        // The list changed under the open modal: refetch it so the
                        // next click works against the current candidates.
                        if (outcome === 'missing') {
                          void queryClient.invalidateQueries({ queryKey: ['skills-audit'] })
                        }
                      }}
                    >
                      {member.name}
                    </Btn>
                    {member.kind === 'pending' && (
                      <span
                        className="shrink-0 rounded border border-border px-1 text-[10px] uppercase tracking-wide"
                        data-testid="skills-audit-pending-badge"
                      >
                        {i18nT('pages.overview.skillsTab.audit_pending_badge')}
                      </span>
                    )}
                  </li>
                ))}
                {omittedMembers > 0 && (
                  <li>{i18nT('pages.overview.skillsTab.audit_more_items', { count: omittedMembers })}</li>
                )}
              </ul>
              {(cluster.omitted_update_targets ?? 0) > 0 && (
                <p className="mt-1 text-[12px] text-muted" data-testid="skills-audit-omitted-update-targets">
                  {i18nT('pages.overview.skillsTab.audit_more_update_targets', {
                    count: cluster.omitted_update_targets ?? 0,
                  })}
                </p>
              )}
            </div>
          )
        })}
        {omittedClusters > 0 && (
          <p className="text-[12px] text-muted" data-testid="skills-audit-omitted-clusters">
            {i18nT('pages.overview.skillsTab.audit_more_items', { count: omittedClusters })}
          </p>
        )}
        {(data?.omitted_entries ?? 0) > 0 && (
          <p className="text-[12px] text-muted" data-testid="skills-audit-omitted-entries">
            {i18nT('pages.overview.skillsTab.audit_more_skills', { count: data?.omitted_entries ?? 0 })}
          </p>
        )}
        {omittedRelations > 0 && (
          <p className="text-[12px] text-muted" data-testid="skills-audit-omitted-relations">
            {i18nT('pages.overview.skillsTab.audit_more_relations', { count: omittedRelations })}
          </p>
        )}

      </div>
    </Modal>
  )
}
