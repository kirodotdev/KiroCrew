import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import Modal from '../../components/Modal'
import { fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

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

export default function SkillsAuditModal({
  open,
  onClose,
  onSelectMember,
  focus,
}: {
  open: boolean
  onClose: () => void
  onSelectMember: (member: SkillAuditMember) => boolean
  focus?: { pendingSlug: string; target: string } | null
}) {
  const [selectionMissed, setSelectionMissed] = useState(false)
  const {
    data,
    isPending,
    error,
  } = useQuery<{ clusters: SkillAuditCluster[] }>({
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

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={i18nT('pages.overview.skillsTab.related_skills')}
      maxWidth={560}
    >
      <div className="space-y-2" data-testid="skills-audit-modal">
        {error && (
          <ErrorNotice
            variant="inline"
            askAgent
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
        {selectionMissed && (
          <p className="rounded-md border border-border bg-bg-elevated p-2 text-[12px] text-muted" role="status">
            {i18nT('pages.overview.skillsTab.audit_member_missing')}
          </p>
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
              <div className="mt-1 flex flex-wrap items-center gap-x-1 text-[12px] text-muted" translate="no">
                {visibleMembers.map((member, index) => (
                  <span key={member.id}>
                    {index > 0 && <span aria-hidden="true"> · </span>}
                    <button
                      type="button"
                      className="text-accent underline underline-offset-2 hover:text-text-strong"
                      onClick={() => setSelectionMissed(!onSelectMember(member))}
                    >
                      {member.name}
                    </button>
                  </span>
                ))}
                {omittedMembers > 0 && (
                  <span>{i18nT('pages.overview.skillsTab.audit_more_items', { count: omittedMembers })}</span>
                )}
              </div>
            </div>
          )
        })}

      </div>
    </Modal>
  )
}
