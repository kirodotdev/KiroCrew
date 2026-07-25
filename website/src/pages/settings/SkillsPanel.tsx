import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api } from '../../api/client'

type SkillsCfg = { auto_create_from_sessions?: boolean; approval_required?: boolean }

/**
 * Settings → Skills: opt in to automatic skill generation from sessions.
 *
 * Auto-generation is OFF by default. When enabled, completed sessions are
 * analyzed and candidate skills are staged to the pending queue (reviewable on
 * the Skills tab) — they never go live without approval unless "Require
 * approval" is turned off.
 */
export function SkillsPanel() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')

  const cfgQ = useQuery<{ skills?: SkillsCfg }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const skills = cfgQ.data?.skills
  const autoCreate = skills?.auto_create_from_sessions ?? false
  // approval_required defaults ON — generated skills stay gated behind review.
  const approvalRequired = skills?.approval_required ?? true

  const patchMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: boolean }) =>
      api.patchConfig(path, value),
    onMutate: async ({ path, value }) => {
      await qc.cancelQueries({ queryKey: ['kirocrewConfig'] })
      const prev = qc.getQueryData<{ skills?: SkillsCfg }>(['kirocrewConfig'])
      const key = path.split('.')[1]
      qc.setQueryData<{ skills?: SkillsCfg }>(['kirocrewConfig'], (old) => ({
        ...(old ?? {}),
        skills: { ...(old?.skills ?? {}), [key]: value },
      }))
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(['kirocrewConfig'], ctx.prev)
      setSaveError('Failed to save skills setting')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })

  const disabled = cfgQ.isLoading || patchMut.isPending

  return (
    <SettingsSection title="Skills">
      <SettingsCard>
        <SettingsToggle
          label="Auto-generate skills from sessions"
          description="Analyze each completed session and draft a reusable SKILL.md when a non-trivial multi-step procedure is detected. Off by default. Drafts are staged to the pending queue on the Skills tab for review — nothing goes live without your approval (see below)."
          checked={autoCreate}
          onChange={(v) => patchMut.mutate({ path: 'skills.auto_create_from_sessions', value: v })}
          disabled={disabled}
        />
        <SettingsToggle
          label="Require approval before generated skills go live"
          description="Keep every auto-generated candidate in the pending queue until you approve it. Turning this off lets prose-only skills publish automatically; skills that bundle scripts always require approval regardless."
          checked={approvalRequired}
          onChange={(v) => patchMut.mutate({ path: 'skills.approval_required', value: v })}
          disabled={disabled || !autoCreate}
        />
      </SettingsCard>
      {saveError && <p className="text-[12px] text-danger mt-2">{saveError}</p>}
    </SettingsSection>
  )
}
