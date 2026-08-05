import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api } from '../../api/client'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
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
      setSaveError(i18nT('pages.settings.skillsPanel.failed_to_save_skills_setting'))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })

  const disabled = cfgQ.isLoading || patchMut.isPending

  return (
    <SettingsSection title={i18nT('pages.settings.skillsPanel.skills')}>
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.skillsPanel.auto_generate_skills_from_sessions')}
          description={i18nT('pages.settings.skillsPanel.analyze_each_completed_session_and_draft_a_reusa')}
          checked={autoCreate}
          onChange={(v) => patchMut.mutate({ path: 'skills.auto_create_from_sessions', value: v })}
          disabled={disabled}
        />
        <SettingsToggle
          label={i18nT('pages.settings.skillsPanel.require_approval_before_generated_skills_go_live')}
          description={i18nT('pages.settings.skillsPanel.keep_every_auto_generated_candidate_in_the_pendi')}
          checked={approvalRequired}
          onChange={(v) => patchMut.mutate({ path: 'skills.approval_required', value: v })}
          disabled={disabled || !autoCreate}
        />
      </SettingsCard>
      <ErrorNotice message={saveError} className="mt-2" askAgent />
    </SettingsSection>
  )
}
