import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, ShieldAlert } from 'lucide-react'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import Modal from './Modal'
import { Btn } from './ui'

// — Consent gate for a project's own `.kiro/skills`.
//
// A SKILL.md is prose, not code, but it enters the agent's context and can
// instruct it to run anything, so loading one out of whatever repository the
// operator happens to have open is an execution-adjacent decision. The copy
// therefore names the CONSEQUENCE rather than explaining the mechanism, and
// both choices state what actually happens — a consent prompt whose decline
// path is unexplained trains reflexive approval.

interface Props {
  open: boolean
  /** Leaf token the operator was trying to use, e.g. "oncall-handover". */
  skillLeaf: string
  /** Real chat-slot key, so the server grants THIS chat's project. */
  slotKey?: string
  onClose: () => void
  /** Called with the leaf once the grant has landed. */
  onTrusted: (leaf: string) => void
}

export default function ProjectSkillsTrustDialog({
  open, skillLeaf, slotKey, onClose, onTrusted,
}: Props) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  // Which directory is actually being trusted. Shown before consent, and sent
  // back with it: the operator must see the folder, and the server refuses the
  // grant if this chat's project has moved on since.
  const { data: trust } = useQuery<{ project?: string | null }>({
    // Same prefix the settings list uses (['skill-trust']), with the slot as a
    // suffix because this snapshot is slot-scoped while the list's is global.
    // One prefix means a single invalidation refreshes both.
    queryKey: ['skill-trust', slotKey ?? null],
    queryFn: () => api.skillTrust(slotKey),
    enabled: open,
  })
  const reviewedPath = trust?.project ?? null

  const confirm = async () => {
    setPending(true)
    setError(null)
    try {
      const snapshot = await api.grantSkillTrust(slotKey, reviewedPath ?? undefined)
      // The catalog's `trusted` flags are now stale — every ['skills', …] entry
      // must be refetched or the row the operator just unlocked still reads as
      // gated. Prefix match covers the per-slot keys.
      await queryClient.invalidateQueries({ queryKey: ['skills'] })
      // Also the trust state: the settings page's trusted-folders list is keyed
      // ['skill-trust', …], and it holds the WITHDRAW control for the folder just
      // granted. Leaving it stale hides the undo on the surface that exists for it
      // — and this dialog reads the same key to show which folder it is trusting.
      await queryClient.invalidateQueries({ queryKey: ['skill-trust'] })
      // Believe the RESPONSE, not merely the absence of an exception. The grant is
      // recorded but `skills.project_skills_enabled` is the operator's hard
      // override, so `trusted` can come back false with no error at all — and
      // reporting success then inserts a $token that expands to nothing, which is
      // exactly the dead token the marked rows exist to avoid.
      if (snapshot?.trusted !== true) {
        setError(i18nT('components.projectSkillsTrust.decline_consequence'))
        return
      }
      onTrusted(skillLeaf)
    } catch (err: unknown) {
      // Duck-typed rather than `instanceof`: a partially-mocked api module
      // leaves the error class undefined, and two bundle realms give the same
      // class different identities.
      const body = (err as { body?: unknown })?.body
      let detail = ''
      if (typeof body === 'string') {
        try {
          detail = String((JSON.parse(body) as { error?: string }).error ?? '')
        } catch {
          detail = ''
        }
      } else if (body && typeof body === 'object') {
        detail = String((body as { error?: string }).error ?? '')
      }
      setError(detail || i18nT('components.projectSkillsTrust.grant_failed'))
    } finally {
      setPending(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      maxWidth={560}
      title={i18nT('components.projectSkillsTrust.title')}
      footer={
        <>
          <Btn onClick={onClose} disabled={pending}>
            {i18nT('components.projectSkillsTrust.decline')}
          </Btn>
          <Btn primary onClick={confirm} disabled={pending || !reviewedPath}>
            {pending
              ? <><Loader2 size={14} className="animate-spin" /> {i18nT('components.projectSkillsTrust.working')}</>
              : <><ShieldAlert size={14} /> {i18nT('components.projectSkillsTrust.confirm')}</>}
          </Btn>
        </>
      }
    >
      <div className="flex flex-col gap-3.5 text-[13px]">
        <p>
          {i18nT('components.projectSkillsTrust.body', { skill: skillLeaf })}
        </p>
        {reviewedPath && (
          <code className="block break-all rounded bg-panel px-2 py-1.5 text-[12px] text-muted">
            {reviewedPath}
          </code>
        )}
        <p className="text-muted">
          {i18nT('components.projectSkillsTrust.consequence')}
        </p>
        <p className="text-muted">
          {i18nT('components.projectSkillsTrust.decline_consequence')}
        </p>
        <p className="text-muted">
          {i18nT('components.projectSkillsTrust.withdraw_hint')}
        </p>
        {error && (
          <p role="alert" className="text-warn">
            {error}
          </p>
        )}
      </div>
    </Modal>
  )
}
